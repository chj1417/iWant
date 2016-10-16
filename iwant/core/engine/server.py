from twisted.internet import reactor, defer, threads, endpoints
from twisted.internet.protocol import Factory
from twisted.protocols.basic import FileSender
from fuzzywuzzy import fuzz, process
import pickle
import os, sys
from fileindexer.findexer import FileHashIndexer
from ..messagebaker import Basemessage
from ..constants import HANDSHAKE, LIST_ALL_FILES, INIT_FILE_REQ, START_TRANSFER, \
        LEADER, DEAD, FILE_SYS_EVENT, HASH_DUMP, SEARCH_REQ, LOOKUP, SEARCH_RES,\
        IWANT_PEER_FILE, SEND_PEER_DETAILS, IWANT, INDEXED, FILE_DETAILS_RESP, \
        ERROR_LIST_ALL_FILES, READY, NOT_READY, PEER_LOOKUP_RESPONSE, LEADER_NOT_READY
from ..protocols import BaseProtocol
from ..config import CLIENT_DAEMON_HOST, CLIENT_DAEMON_PORT, SERVER_DAEMON_PORT


class ServerException(Exception):
    def __init__(self, code, msg):
        self.code = code
        self.msg = msg

    def __str__(self):
        return 'Error [{0}] => {1}'.format(self.code, self.msg)

class backend(BaseProtocol):
    def __init__(self, factory):
        self.factory = factory
        self.message_codes = {
            HANDSHAKE: self._handshake,
            LIST_ALL_FILES: self._list_file,
            INIT_FILE_REQ: self._load_file,
            START_TRANSFER: self._start_transfer,
            LEADER: self._update_leader,
            DEAD  : self._remove_dead_entry,
            FILE_SYS_EVENT: self._filesystem_modified,
            HASH_DUMP: self._dump_data_from_peers,
            SEARCH_REQ: self._leader_send_list,
            LOOKUP: self._leader_lookup,
            SEARCH_RES: self._send_resp_client,
            IWANT_PEER_FILE: self._ask_leader_for_peers,
            SEND_PEER_DETAILS: self._leader_looksup_peer,
            IWANT: self._start_transfer,
            INDEXED : self.fileindexing_complete
        }
        self.buff = ''
        self.delimiter = '#'
        self.special_handler = None

    def serviceMessage(self, data):
        '''
            Controller which processes the incoming messages and invokes the appropriate functions
        '''
        req = Basemessage(message=data)
        try:
            self.message_codes[req.key]()
        except:
            self.message_codes[req.key](req.data)

    def leaderThere(self):
        '''
            Tells if leader is present in the network or not
        '''
        if self.factory.leader is not None:
            return True
        else:
            return False

    def _handshake(self):
        # TODO: unused
        resMessage = Basemessage(key=HANDSHAKE, data=[])
        self.sendLine(resMessage)

    def _list_file(self):
        # TODO: unused
        if self.factory.state == READY:
            resMessage = Basemessage(key=LIST_ALL_FILES, data=self.factory.indexer.reduced_index())
            self.sendLine(resMessage)
        else:
            resMessage = Basemessage(key=ERROR_LIST_ALL_FILES, data='File hashing incomplete')
            self.sendLine(resMessage)

    def _load_file(self, data):
        fhash = data
        if self.factory.state == READY:
            self.fileObj = self.factory.indexer.getFile(fhash)
            fname, _, fsize = self.factory.indexer.hash_index[fhash]
            print fhash, fname, fsize
            ack_msg = Basemessage(key=FILE_DETAILS_RESP, data=(fname, fsize))
            self.sendLine(ack_msg)
        else:
            print 'files not indexed yet'

    def _start_transfer(self, data):
        producer = FileSender()
        consumer = self.transport
        fhash = data
        fileObj = self.factory.indexer.getFile(fhash)
        deferred = producer.beginFileTransfer(fileObj, consumer)
        deferred.addCallbacks(self._success, self._failure)

    def _success(self, data):
        self.transport.loseConnection()
        self.unhookHandler()

    def _failure(self, reason):
        print 'Failed {0}'.format(reason)
        self.transport.loseConnection()
        self.unhookHandler()

    def _update_leader(self, leader):
        self.factory.leader = leader
        print 'Updating Leader {0}'.format(self.factory.book.leader)
        if self.factory.state == READY and self.leaderThere():
            self.factory.gather_data_then_notify()

    def _filesystem_modified(self, data):
        if self.factory.state == READY and self.leaderThere():
            self.factory.gather_data_then_notify()
        else:
            if self.factory.state == NOT_READY:
                resMessage = Basemessage(key=ERROR_LIST_ALL_FILES, data='File hashing incomplete')
                self.sendLine(resMessage)
            else:
                msg = Basemessage(key=LEADER_NOT_READY, data=None)
                self.sendLine(msg)
            self.transport.loseConnection()

    def _dump_data_from_peers(self, data):
        uuid, dump = data
        self.factory.data_from_peers[uuid] = dump

    def _remove_dead_entry(self, data):
        uuid = data
        print '@server: removing entry {0}'.format(uuid)
        try:
            del self.factory.data_from_peers[uuid]
        except:
            raise ServerException(1, '{0} not available in cached data'.format(uuid))

    def _leader_send_list(self, data):
        if self.leaderThere():
            print 'lookup request sent to leader'
            self.factory._notify_leader(key=LOOKUP, data=data, persist=True, clientConn=self)
        else:
            msg = Basemessage(key=LEADER_NOT_READY, data=None)
            self.sendLine(msg)
            self.transport.loseConnection()

    def _leader_lookup(self, data):
        print 'damn i have to look up '
        uuid, text_search = data
        filtered_response = []
        l = []
        print ' the length of data_from_peers : {0}'.format(len(self.factory.data_from_peers.values()))
        if len(self.factory.data_from_peers.values()) != 0:
            for val in self.factory.data_from_peers.values():
                l = pickle.loads(val['hidx'])
                for i in l.values():
                    if fuzz.partial_ratio(text_search.lower(), i.filename.lower()) >= 90:
                        filtered_response.append(i)
        else:
            filtered_response = []
        update_msg = Basemessage(key=SEARCH_RES, data=filtered_response)
        self.sendLine(update_msg)  # this we are sending it back to the server
        self.transport.loseConnection()  # leader will loseConnection with the requesting server

    def _send_resp_client(self, data):
        #TODO : unused
        update_msg = Basemessage(key=SEARCH_RES, data=data)
        self.sendLine(update_msg)  # sending this response to the client
        self.transport.loseConnection()  # losing connection with the client

    def _ask_leader_for_peers(self, data):
        if self.leaderThere():
            #print 'asking leaders for peers'
            print data
            self.factory._notify_leader(key=SEND_PEER_DETAILS, data=data, persist=True, clientConn=self)
        else:
            msg = Basemessage(key=LEADER_NOT_READY, data=None)
            self.sendLine(msg)
            self.transport.loseConnection()

    def _leader_looksup_peer(self, data):
        uuids = []
        sending_data = []

        for key, val in self.factory.data_from_peers.iteritems():
            if data in pickle.loads(val['hidx']):
                uuids.append(key)

        for uuid in uuids:
            sending_data.append(self.factory.book.peers[uuid])
        msg = Basemessage(key=PEER_LOOKUP_RESPONSE, data=sending_data)
        self.sendLine(msg)
        self.transport.loseConnection()

    def fileindexing_complete(self):
        self.factory.state = READY
        self.factory.indexer = FileHashIndexer(self.factory.folder,\
                self.factory.config_folder)
        self.factory.gather_data_then_notify()


class backendFactory(Factory):

    protocol = backend

    def __init__(self, book, sharing_folder=None, download_folder=None, config_folder=None):
        self.state = NOT_READY  # file indexing state
        self.folder = sharing_folder
        self.download_folder = download_folder
        self.config_folder = config_folder
        self.book = book
        self.leader = None
        self.cached_data = None
        self.data_from_peers = {}
        self.indexer = None  # FileHashIndexer(self.folder, self.config_folder)

    def clientConnectionLost(self, connector, reason):
        print 'Lost connection'

    def gather_data_then_notify(self):
        self.cached_data = {}
        hidx_file = os.path.join(self.config_folder, '.hindex')
        pidx_file = os.path.join(self.config_folder, '.pindex')
        with open(hidx_file) as f:
            hidx = f.read()
        with open(pidx_file) as f:
            pidx = f.read()
        self.cached_data['hidx'] = hidx
        self.cached_data['pidx'] = pidx
        self._notify_leader(key=HASH_DUMP, data=None)

    def _notify_leader(self, key=None, data=None, persist=False, clientConn=None):
        from twisted.internet.protocol import Protocol, ClientFactory
        from twisted.internet import reactor

        class ServerLeaderProtocol(BaseProtocol):
            def __init__(self, factory):
                self.buff = ''
                self.delimiter = '#'
                self.special_handler = None
                self.factory = factory
                self.events = {
                    PEER_LOOKUP_RESPONSE: self.talk_to_peer,
                    SEARCH_RES: self.send_file_search_response
                }

            def connectionMade(self):
                update_msg = Basemessage(key=self.factory.key, data=self.factory.dump)
                self.transport.write(str(update_msg))
                if not persist:
                    self.transport.loseConnection()

            def serviceMessage(self, data):
                req = Basemessage(message=data)
                try:
                    self.events[req.key]()
                except:
                    self.events[req.key](req.data)

            def talk_to_peer(self, data):
                from twisted.internet.protocol import Protocol, ClientFactory, Factory
                from twisted.internet import reactor
                self.transport.loseConnection()
                print 'Got peers {0}'.format(data)
                if len(data) == 0:
                    print 'Tell the client that peer lookup response is 0. Have to handle this'
                host, port = data[0]
                print 'hash {0}'.format(self.factory.dump)
                print self.factory.dump_folder
                from ..protocols import RemotepeerFactory, RemotepeerProtocol
                reactor.connectTCP(host, SERVER_DAEMON_PORT, RemotepeerFactory(INIT_FILE_REQ, self.factory.dump, clientConn, self.factory.dump_folder))

            def send_file_search_response(self, data):
                update_msg = Basemessage(key=SEARCH_RES, data=data)
                clientConn.sendLine(update_msg)
                clientConn.transport.loseConnection()

        class ServerLeaderFactory(ClientFactory):
            def __init__(self, key, dump, dump_folder=None):
                self.key = key
                self.dump = dump
                if dump_folder is not None:
                    self.dump_folder = dump_folder

            def buildProtocol(self, addr):
                return ServerLeaderProtocol(self)

        if key == HASH_DUMP:
            factory = ServerLeaderFactory(key=key, dump=(self.book.uuidObj, self.cached_data))
        elif key == LOOKUP:
            factory = ServerLeaderFactory(key=key, dump=(self.book.uuidObj, data))
        elif key == SEND_PEER_DETAILS:
            factory = ServerLeaderFactory(key=key, dump=data, dump_folder = self.download_folder)

        if key == SEND_PEER_DETAILS or key == LOOKUP:
            if self.leader is not None:
                host, port = self.leader[0] , self.leader[1]
                print 'connecting to {0}:{1} for {2}'.format(host, port, key)
                reactor.connectTCP(host, port, factory)
        elif key == HASH_DUMP:
            if self.leader is not None and self.state == READY:
                host, port = self.leader[0] , self.leader[1]
                print 'connecting to {0}:{1} for {2}'.format(host, port, key)
                reactor.connectTCP(host, port, factory)

    #def _file_hash_failure(self, reason):
    #    print reason
    #    raise NotImplementedError

    def buildProtocol(self, addr):
        return backend(self)

    def connectionMade(self):
        print 'connection established'