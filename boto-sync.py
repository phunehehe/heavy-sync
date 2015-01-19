#!/usr/bin/env python

from boto.exception import GSResponseError
from boto.exception import S3ResponseError
from boto.gs.key import Key as GSKey
from boto.s3.key import Key as S3Key
from multiprocessing.pool import ThreadPool
from os import path, rename
from time import time

import argparse
import boto
import socket
import sqlite3
import sys
import tempfile

# Seemingly unused but needed for GCS authentication
import gcs_oauth2_boto_plugin


def get_bucket(bucket_uri, validate=False):
    scheme = bucket_uri[:2]
    bucket_name = bucket_uri[5:]
    connection = {
        'gs': boto.connect_gs,
        's3': boto.connect_s3,
    }[scheme]()
    return connection.get_bucket(bucket_name, validate=validate)


def get_key(bucket_uri, path):
    scheme = bucket_uri[:2]
    constructor = {
        'gs': GSKey,
        's3': S3Key,
    }[scheme]
    return constructor(get_bucket(bucket_uri), path)


# Download an object from source bucket, then upload it to destination bucket
def transfer(source, destination, path):
    while True:
        try:
            # Roll over when hitting 10 MB
            f = tempfile.SpooledTemporaryFile(max_size=10*2**20)
            get_key(source, path).get_contents_to_file(f)
            get_key(destination, path).set_contents_from_file(f, rewind=True)
            return path
        except socket.error as e:
            print e
        except (GSResponseError, S3ResponseError) as e:
            if e.status == 404:
                # TODO: Log this as a warning somewhere
                return path
            else:
                raise


# Remove an object from destination bucket, ignoring "not found" errors
def remove(destination, path):
    bucket = get_bucket(destination)
    try:
        bucket.delete_key(path)
    except (GSResponseError, S3ResponseError) as e:
        if e.status != 404:
            raise


def finished(connection):
    cursor = connection.cursor()
    cursor.execute('''SELECT 1 FROM source WHERE NOT processed LIMIT 1''')
    return cursor.fetchone() is None


def process(source, destination, connection):

    print 'Skipping over up-to-date objects...'
    connection.execute('''
        UPDATE source SET processed = 1
        WHERE rowid IN (
            SELECT s.rowid FROM source s JOIN destination d
            ON s.path = d.path AND s.hash = d.hash
        )
    ''')

    print 'Uploading new/updated objects from source to destination...'
    while not finished(connection):
        sql_it = connection.execute('''SELECT path FROM source WHERE NOT processed LIMIT 1000''')
        pool = ThreadPool()
        pool_it = pool.imap_unordered(lambda row: transfer(source, destination, row[0]),
                                      list(sql_it))
        for path in pool_it:
            connection.execute('''UPDATE source SET processed = 1 WHERE path = ?''', (path,))
            print 'Finished: %s/%s -> %s/%s' % (source, path, destination, path)

    print 'Deleting objects in destination that have been deleted in source...'
    for row in connection.execute('''
        SELECT d.rowid, d.path
        FROM destination d LEFT JOIN source s
        ON d.path = s.path WHERE s.path IS NULL
    '''):
        remove(destination, row[1])
        connection.execute('''DELETE FROM destination WHERE rowid = ?''', (row[0],))


# Populate the table with the contents of the bucket
def get_contents(bucket_uri, connection, table):
    bucket = get_bucket(bucket_uri, validate=True)
    for key in bucket.list():
        connection.execute('INSERT INTO %s (bucket, path, hash) VALUES (?, ?, ?)' % table,
                           (bucket_uri, key.name, key.etag))


def initialize_db(connection):

    connection.executescript('''
        CREATE TABLE source (bucket VARCHAR,
                             path VARCHAR,
                             hash VARCHAR,
                             processed BOOLEAN DEFAULT 0);
        CREATE TABLE destination (bucket VARCHAR, path VARCHAR, hash VARCHAR);
        CREATE INDEX IF NOT EXISTS source_path_index ON source (path);
        CREATE INDEX IF NOT EXISTS source_hash_index ON source (hash);
        CREATE INDEX IF NOT EXISTS destination_path_index ON destination (path);
        CREATE INDEX IF NOT EXISTS destination_hash_index ON destination (hash);
    ''')


def new_run(source, destination, db):
    print 'Starting a new run...'
    connection = sqlite3.connect(db, isolation_level=None)
    initialize_db(connection)
    get_contents(destination, connection, 'destination')
    get_contents(source, connection, 'source')
    process(source, destination, connection)


def resume(source, destination, connection):
    print 'Resuming a previous run...'
    process(source, destination, connection)


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('destination')
    parser.add_argument('--db', default='state.db')
    args = parser.parse_args()

    db = args.db

    if path.exists(db):

        try:
            connection = sqlite3.connect(db, isolation_level=None)
            if finished(connection):
                print 'Backing up previous completed run...'
                connection.close()
                rename(db, '%s-%d' % (db, int(time())))
                new_run(args.source, args.destination, db)
            else:
                resume(args.source, args.destination, connection)

        except sqlite3.OperationalError as e:
            print 'Error encountered, please clean up %s manually.' % db
            raise

    else:
        new_run(args.source, args.destination, db)


main()
