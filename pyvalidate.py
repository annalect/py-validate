from ConfigParser import *
import os, re, sys, json, io, atexit
import boto3
import time, datetime, traceback

def dictFromConfig(filepath):
    config = ConfigParser()
    config.read(filepath)
    return {section.lower(): dict(config.items(section)) for section in config.sections()}

class parameters(object):
    def __init__(self, args):
    # if at first you don't succeed,
        self.args = args
        self.keys = dictFromConfig('/serverbase.cfg')
        # input is assumed to be JSON string piped to stdin. If stdin is a terminal, this skips right over, defaulting to an empty JSON object.
        # when called as subprocess in a larger server, isatty will also be false, so it will attempt to read stdin, which will block if there's no end byte available. So pipe a null char for goodness sake.
        self.result = {'stdin': sys.stdin.read() if not sys.stdin.isatty() else "{}" }
        self.input = json.loads(self.result.get('stdin'))
        self.typecast = {
            # type is HTML form input type : followed by python type. file buffers are streams, s3 is an s3Object: .put(Body=STINGIO) to upload
            'number::int':   lambda x: int(x),
            'number::float': lambda x: float(x),

            'date::date':    lambda x: datetime.datetime.strptime(x,'%Y-%m-%d'),                
            
            'text::str':     lambda x: str(x),
            'text::unicode': lambda x: unicode(x),
            'text::tuple':   lambda x: tuple([i.strip() for i in x.split(',')]),
            'text::bool':    lambda x: True if x.lower() == 'true' else False,
            
            'file::buffer':  lambda x: io.open(x, mode='rt'), # named input, assumes file exists, renders as file upload form, open file for reading
            'text::buffer':  lambda x: io.open(x, mode='wb'), # named output, creates a new file (or overwrites existing), renders as text input, open file for writing
                                # would be nice to add a getter for 'this.s3.GetObject...' 'this.s3.Object()' to stick the key getting somewhere else
            'file::s3':      lambda x: self.s3().get('readable')(x), # should be able to READ this!
            'text::s3':      lambda x: self.s3().get('writable')(x)
        
        }
        # Evaluate each of the keys
        # Check if there's a database, check if connection can be established
        # 
        for key in self.input:
            if key in self.args:
                self.args[key]['value'] = self.input[key]
        # try to access each required property in the input json
        for key in self.args:
            if key == 'database':
                self.makeDatabaseFrom(self.args[key])
            else:
                # the key may exist in input, or it may have been defined in the required object
                argValue = str(self.args[key].get('value'))
                argType = self.args[key].get('type') # if there's no type, should throw error, malformed input
                # this if not/if/else/else/if/else either sets input value or throws an error. godspeed.
                self.stdout("Using '" + argValue + "' for " + key + "\n")
                if('verify' in self.args[key]):
                    argVerify = re.compile(self.args[key]['verify'])
                    match = argVerify.findall(argValue)
                    if(len(match) == 0):
                        raise SyntaxError(argValue + ' did not appear to be ' 
                                                            + self.args[key]['info'] 
                                                            + '\nRegex Failed To Match:\n' 
                                                            + self.args[key]['verify']
                                                            + '\n' + self.args[key].get('help','') + '\n')
                
                self.__dict__[key] = self.typecast[argType](argValue)

    #####  end of constructor ######
    def get(self, key, default):
        return self.__dict__.get(key, default)

    def s3(self):
        bucket = self.keys['s3']['bucket']
        prefix = self.keys['s3']['prefix']
        dest_key = prefix + 'id/' + os.environ.get('USER','nobody') + '/'
        resource = boto3.resource('s3',
            aws_access_key_id=self.keys['s3']['aws_access_key_id'],
            aws_secret_access_key=self.keys['s3']['aws_secret_access_key']
        )
        return {
            # readable takes a full path and splits it into bucket / prefix...
            'readable': lambda x: resource.GetObject(x.split('/')[0], x.split('/')[1:]),
            # writeable defines a destination  # you can .put(Body=STRING.IO) to thing!
            'writable': lambda x: resource.Object(bucket, dest_key + x)
        }

    def stderr(self, data):
        self.output({"stderr": str(data) + '\n'})

    def stdout(self, data):
        self.output({"stdout" : str(data) + '\n'})

    def output(self, stringOrDict):
        # strings passed to output will be wrapped in an object with key 'stdout'
        newData = stringOrDict if type(stringOrDict) == dict else {"stdout": stringOrDict + '\n'}
        for key in newData:
            if self.result.get(key) == None:
                # create key if it doesn't exist
                self.result[key] = newData[key]
            else:
                # append new data to existing key
                self.result[key] += newData[key]
        # output is buffered by default,
        # if that's the case, wait til program exit to print one single object
        # but if PYTHONUNBUFFERED is specified, print out each piece of data as it comes as separate JSON object
        # useful for streaming API
        if os.environ.get('PYTHONUNBUFFERED'):
            print(json.dumps(newData))
        else:
            atexit.register(self.outputOnExit)
            #would be better to only register once, otherwise this gets called as many times as output was written which is pretty bogus.
    
    def outputOnExit(self):
        if self.result:
            print(json.dumps(self.result))
            self.result = None # invalidate object so it only happens once. 
    
    def makeDatabaseFrom(self, databaseDict):
        database = None # database defaults to none if psql or mysql database is not named
        dbKeys = {}
        # should merge databaseDict with dbKeys to check that I have name, port, host, password...
        # thankfully psycopg2 and pymysql use the same query api
        if databaseDict.get('psql'):
            import psycopg2 as database
            dbKeys = self.keys.get(databaseDict['psql'])
        elif databaseDict.get('mysql'):
            import pymysql as database
            dbKeys = self.keys.get(databaseDict['mysql'])
            dbKeys['port'] = int(dbKeys['port'])

        # only load connection if database was named. name must correspond with a serverbase.cfg section.
        if database and dbKeys:
            try:
                self.database = database.connect(**dbKeys)
                self.stdout("Connection Established")
            except Exception as e:
                self.stderr("Unable to connect to the database")
                self.stderr(str(e))
                sys.exit()
        else:
            self.stderr("database object should look like {psql or mysql: name of key section}")