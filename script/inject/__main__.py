#!/usr/bin/python
# -*- coding: utf-8 -*-


import requests
import json
import re
import subprocess
import multiprocessing
import sqlite3
import datetime
import sys
import time
import flask
import signal, os
import random
from itertools import repeat


app = flask.Flask(__name__)
app.secret_key = 'oaisj84alwsdkjhf9238u'
staleTime = datetime.timedelta(7,0,0) # one week for now

def getDb(makeDates=True):
    if makeDates:
        return sqlite3.connect('./items.db', detect_types=sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES, timeout=30.0 )
    else:
        return sqlite3.connect('./items.db', timeout=30.0 )


def handler(signum, frame):
    print 'Signal handler called with signal', signum
    raise IOError("should kill the thread!")

#Generate environment with:
#pex -r requests -r multiprocessing -e inject:main -o warc-inject -s '.' --no-wheel
#pex -r requests -r multiprocessing -o warc-inject
# import argparse
# parser = argparse.ArgumentParser()
# parser.add_argument('--foo', help='foo help')
# args = parser.parse_args()

def reallyExecute(c, query, qargs):
    while True:
        try:
            res = c.execute(query, qargs)
            #c.commit() # Getting database is locked errors, will this help?
            return res
        except sqlite3.OperationalError, e:
            time.sleep(1)
            print 'got locked database %s,%s, retrying (%s)' % (query,qargs,e)
            continue

def reallyExecuteMany(c, query, qargs):
    while True:
        try:
            res = c.executemany(query, qargs)
            #c.commit() # Getting database is locked errors, will this help?
            return res
        except sqlite3.OperationalError:
            time.sleep(1)
            print 'got locked database %s, retrying' % query
            continue

    

def injectItem(item, db, mode):
    c = db.cursor()
    res = reallyExecute(c, 'select * from items where item = ?', (item,)).fetchone()
    itemId = None
    if res:
        if res[1] > (datetime.datetime.now() - staleTime):
            print 'skipping %s because we checked recently' % item
            return 0     # We checked recently
        itemId = res[0]


    while True:
        try:
            metadata = subprocess.Popen(['./ia','metadata', item],
                                        stdout=subprocess.PIPE).communicate()[0]
            #print 'item metadata is ', metadata, 'item is ', item
            md = json.loads(metadata)
            break
        except Exception, e:
            print 'error: metadata feed went down (%s) for: %s' % (e, item)
            time.sleep(10)


    if itemId is None:
        reallyExecute(c, "insert INTO items VALUES (?,?)", (item, datetime.datetime.now()))
        itemId = c.lastrowid
        db.commit()

    if 'files' not in md:
        return

    res = None
    res = reallyExecute(c, "select fileName, updated, status, took from files where itemId = ?", 
                        (itemId,)).fetchall()

    lastUpdate = {}
    for fileName, updated, status, took in res:
        if status == -1: # Auto retry if we couldn't reach gb last time
            continue
        lastUpdate[fileName] = updated

    dbUpdates = []
    skipped = 0
    for ff in md['files']:
        if not ff['name'].endswith('arc.gz'): continue
        itemMetadata = {'mtime':ff['mtime']}
        updateTime = datetime.datetime.fromtimestamp(float(ff['mtime']))
        if ff['name'] in lastUpdate and updateTime <= lastUpdate[ff['name']]:
            print "skip {0} because it is up to date".format(ff['name'])
            skipped += 1
            continue
        
        itemMetadata.update(md['metadata'])
        postVars = {'url':'http://archive.org/download/%s/%s' %
                    (item,ff['name']),
                    'metadata':json.dumps(itemMetadata),
                    'c':'ait',
                    'spiderlinks':0}
        start = time.time()
        if mode == 'production':
            try:
                rp = requests.post("http://localhost:8000/admin/inject", postVars)
                statusCode = rp.status_code

            except requests.exceptions.ConnectionError, e:
                print 'error: gb inject', postVars['url'], e
                statusCode = -1
            #print postVars['url'], rp.status_code
        else:
            time.sleep(random.randint(1,4))
            statusCode = 999
        took = time.time() - start

        print "sent", ff['name'],'to gb, took', took
        sys.stdout.flush()
            
        dbUpdates.append((itemId, ff['name'], updateTime, statusCode, took))

    reallyExecuteMany(c, "DELETE FROM files where fileName = ? ", zip(lastUpdate.iterkeys()))
    reallyExecuteMany(c, "INSERT INTO files VALUES (?,?,?,?,?)",
                      dbUpdates)
    db.commit()

    print 'completed %s with %s items injected and %s skipped' % (item, len(dbUpdates), skipped)

def getPage(zippedArgs):
    page, mode = zippedArgs
    #r = requests.get('https://archive.org/advancedsearch.php?q=collection%3Aarchiveitdigitalcollection&fl%5B%5D=identifier&rows=1&page={0}&output=json&save=yes'.format(page))
    r = requests.get('https://archive.org/advancedsearch.php?q=collection%3Aarchiveitdigitalcollection&fl%5B%5D=identifier&sort[]=date+desc&rows=100&page={0}&output=json&save=yes'.format(page))
    if r.status_code != 200:
        return 0

    contents = r.content
    jsonContents = json.loads(contents)
    items = [x['identifier'] for x in jsonContents['response']['docs']]
    numFound = jsonContents['response']['numFound']
                  
    if len(items) == 0:
        print 'got 0 items for search page', page
        return 0
    print 'loading %s items, %s - %s of %s' % (len(items), items[0], items[-1], numFound)

    db = getDb()

    for item in items:
        injectItem(item, db, mode)

    db.close()
    return len(items)




def dumpDb():
    db = getDb()
    c = db.cursor()
    res = c.execute("select * from files")
    for (itemId, fileName, updated, status, took) in res.fetchall():
        print 'xxx',itemId, fileName, updated, status, took

    res = c.execute("select ROWID, item, checked from items")
    for (rid, item, checked) in res.fetchall():
        print 'yyy',(rid, item, checked)
    db.close()


def showItems():
    db = getDb()
    c = db.cursor()
    res = c.execute("select distinct item from items")    
    for item, in res:
        print item
    db.close()


def nuke(lastPid, fromOrbit=False):
    sig = signal.SIGTERM
    if fromOrbit:
        sig = signal.SIGKILL
        
    if lastPid is not None:
        try:
            ret = os.kill(int(lastPid), signal.SIGTERM)
            print 'killing ', ret
            return 
        except:
            pass

    killed = subprocess.Popen("""kill `ps auxx |grep warc-inject|awk -e '{print $2}'`""" % sys.argv[0],
                              shell=True,stdout=subprocess.PIPE).communicate()[0]

    if killed == 'Terminated':
        print 'got it'
        return
    print 'missed', killed


def main():
    try:
        lastPid = open('running.pid', 'r').read()
    except:
        lastPid = None
    print 'arguments were', sys.argv, 'pid is', os.getpid()
    open('running.pid', 'w').write(str(os.getpid()))
    if len(sys.argv) == 2:
        if sys.argv[1] == 'init':
            init()
            print 'initialized'
            return sys.exit(0)
        if sys.argv[1] == 'reset':
            os.unlink('items.db')
            init()
            return sys.exit(0)
        if sys.argv[1] == 'dump':
            dumpDb()

        if sys.argv[1] == 'items':
            showItems()

        if sys.argv[1] == 'stop':
            nuke(lastPid)

        if sys.argv[1] == 'kill':
            nuke(lastPid, fromOrbit=True)

        if sys.argv[1] == 'test':
            mode = 'testing'
            runInjects(10, 'testing')

        if sys.argv[1] == 'migrate':
            db = getDb()
            c = db.cursor()
            c.execute('ALTER TABLE items RENAME TO old')
            db.commit()
            
            c.execute('''CREATE TABLE files
            (itemId TEXT, fileName TEXT, updated TIMESTAMP, status INTEGER, took FLOAT)''')

            c.execute('''CREATE TABLE items
            (item text, checked timestamp)''')

            c.execute('''CREATE INDEX item_index ON items (item)''')



            # res = c.execute("select count(*) from old")
            # print list(res)
            #res = c.execute("select distinct item from items")    
            alreadyItem = {}
            # res = c.execute("select * from old")
            # print (len(list(res)))
            res = c.execute("select * from old")
            now = datetime.datetime.now()
            for (item, fileName, updated, status, took) in res.fetchall():
                #print 'inserting row', item

                if item not in alreadyItem: 
                    c.execute("insert INTO items VALUES (?,?)", (item, now))

                alreadyItem[item] = c.lastrowid

                c.execute("INSERT INTO files VALUES (?,?,?,?,?)", 
                          (alreadyItem[item], fileName, updated, status, took))

            c.execute('''drop table old''')

            db.commit()
            db.close()
            dumpDb()
            return

        if sys.argv[1] == 'testsig':
            def handler(signum, frame):
                print 'Signal handler called with signal', signum
                raise IOError("Couldn't open device!")

            # Set the signal handler and a 5-second alarm
        
            signal.signal(signal.SIGTERM, handler)
            #signal.alarm(5)

            # This open() may hang indefinitely
            time.sleep(100)
            #fd = os.open('/dev/ttyS0', os.O_RDWR)

            signal.alarm(0)          # Disable the alarm



        if sys.argv[1] == 'serve':
            serveForever()

    if len(sys.argv) == 3:

        if sys.argv[1] == 'force':
            itemName = sys.argv[2]
            db = getDb()
            injectItem(itemName, db, 'production')
            sys.exit(0)

    if len(sys.argv) == 4:
        if sys.argv[1] == 'forcefile':
            global staleTime
            staleTime = datetime.timedelta(0,0,0)
            from multiprocessing.pool import ThreadPool
            fileName = sys.argv[2]
            items = filter(lambda x: x, open(fileName, 'r').read().split('\n'))
            threads = int(sys.argv[3])
            pool = ThreadPool(processes=threads)
            #print zip(files, repeat(getDb(), len(files)), repeat('production', len(files)))
            def injectItemTupleWrapper(itemName):
                db = getDb()
                ret = injectItem(itemName, db, 'production')
                db.close()
                return ret

            answer = pool.map(injectItemTupleWrapper, items)
            sys.exit(0)

        if sys.argv[1] == 'injectitems':
            from multiprocessing.pool import ThreadPool
            fileName = sys.argv[2]
            items = filter(lambda x: x, open(fileName, 'r').read().split('\n'))
            threads = int(sys.argv[3])
            pool = ThreadPool(processes=threads)
            #print zip(files, repeat(getDb(), len(files)), repeat('production', len(files)))
            def injectItemTupleWrapper(itemName):
                db = getDb()
                ret = injectItem(itemName, db, 'production')
                db.close()
                return ret

            answer = pool.map(injectItemTupleWrapper, items)
            sys.exit(0)



        if sys.argv[1] == 'run':
            threads = int(sys.argv[2])
            runInjects(threads)

        
def runInjects(threads, mode='production'):
    from multiprocessing.pool import ThreadPool
    pool = ThreadPool(processes=threads)
    try:
        maxPages = 1300
        answer = pool.map(getPage, zip(xrange(1,maxPages), repeat(mode, maxPages)))
        print "finished item pass", answer
    except (KeyboardInterrupt, SystemExit):
        print 'ok, caught'
        raise


def init():
    db = getDb()
    c = db.cursor()
    c.execute('''CREATE TABLE files
    (itemId TEXT, fileName TEXT, updated TIMESTAMP, status INTEGER, took FLOAT)''')

    c.execute('''CREATE TABLE items
    (item text, checked timestamp)''')

    c.execute('''CREATE INDEX item_index ON items (item)''')
    db.commit()
    db.close()


def serveForever():
    @app.route('/',
               methods=['GET', 'POST'], endpoint='home')
    def home():
        db = getDb(makeDates=False)
        res = db.execute('select * from items limit 10')
        for item, checked in res.fetchall():
            print item
            try:
                metadata = subprocess.Popen(['./ia','metadata', item],
                                            stdout=subprocess.PIPE).communicate()[0]

                break
            except Exception, e:
                pass
        db.close()







        return flask.make_response(metadata)

    @app.route('/progress',
               methods=['GET', 'POST'], endpoint='progress')
    def progress():
        r = requests.get('https://archive.org/advancedsearch.php?q=collection%3Aarchiveitdigitalcollection&fl%5B%5D=identifier&sort[]=date+desc&rows=1&page=1&output=json')
        if r.status_code != 200:
            return flask.make_response(json.dumps({error:'ia search feed is down'}), 
                                       'application/json')

        contents = r.content
        jsonContents = json.loads(contents)
        numFound = jsonContents['response']['numFound']


        db = getDb()
        examinedItems = db.execute('select count(*) from items').fetchone()
        itemsWithWarc = db.execute('select count(*) from items where ROWID in (select itemId from files where files.status = 200)').fetchone()
        return flask.make_response(json.dumps({'totalItems':numFound, 
                                               'examinedItems':examinedItems,
                                               'itemsWithWarc':itemsWithWarc
                                           }, indent=4), 'application/json')


    @app.route('/items',
               methods=['GET', 'POST'], endpoint='items')
    def items():
        db = getDb(makeDates=False)

        c = db.cursor()
        res = c.execute("select item, checked from items")    

        out = []
        for item, checked in res.fetchall():
            out.append({'item':item, 'checked':checked})
        db.close()

        return flask.make_response(json.dumps(out), 'application/json')

    app.run('0.0.0.0', 
            port=7999,
            debug=True,
            use_reloader=True,
            use_debugger=True)


if __name__ == '__main__':
    main()


