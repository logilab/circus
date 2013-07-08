import errno
import logging
import os
from threading import Thread, RLock
from thread import get_ident
import sys
from time import sleep
import select
import socket

import zmq
from zmq.eventloop import ioloop

from circus.controller import Controller
from circus.exc import AlreadyExist
from circus import logger
from circus.watcher import Watcher
from circus.util import debuglog, _setproctitle
from circus.config import get_config
from circus.plugins import get_plugin_cmd
from circus.sockets import CircusSocket, CircusSockets


class ReloadArbiterException(Exception):
    pass


class Arbiter(object):
    """Class used to control a list of watchers.

    Options:

    - **watchers** -- a list of Watcher objects
    - **endpoint** -- the controller ZMQ endpoint
    - **pubsub_endpoint** -- the pubsub endpoint
    - **statsd** -- If True, a circusd-stats process is run (default: False)
    - **stats_endpoint** -- the stats endpoint.
    - **statsd_close_outputs** -- if True sends the circusd-stats stdout/stderr
      to /dev/null (default: False)
    - **multicast_endpoint** -- the multicast endpoint for circusd cluster
      auto-discovery (default: udp://237.219.251.97:12027)
      Multicast addr should be between 224.0.0.0 to 239.255.255.255 and the
      same for the all cluster.
    - **check_delay** -- the delay between two controller points
      (default: 1 s)
    - **prereload_fn** -- callable that will be executed on each reload
      (default: None)
    - **context** -- if provided, the zmq context to reuse.
      (default: None)
    - **loop**: if provided, a :class:`zmq.eventloop.ioloop.IOLoop` instance
       to reuse. (default: None)
    - **plugins** -- a list of plugins. Each item is a mapping with:

        - **use** -- Fully qualified name that points to the plugin class
        - every other value is passed to the plugin in the **config** option
    - **sockets** -- a mapping of sockets. Each key is the socket name,
      and each value a :class:`CircusSocket` class. (default: None)
    - **warmup_delay** -- a delay in seconds between two watchers startup.
      (default: 0)
    - **httpd** -- If True, a circushttpd process is run (default: False)
    - **httpd_host** -- the circushttpd host (default: localhost)
    - **httpd_port** -- the circushttpd port (default: 8080)
    - **httpd_close_outputs** -- if True, sends circushttpd stdout/stderr
      to /dev/null. (default: False)
    - **debug** -- if True, adds a lot of debug info in the stdout (default:
      False)
    - **proc_name** -- the arbiter process name
    - **fqdn** -- a unique identifier for the machine where circus runs.
    """
    def __init__(self, watchers, endpoint, pubsub_endpoint, check_delay=.5,
                 prereload_fn=None, context=None, loop=None, statsd=False,
                 stats_endpoint=None, statsd_close_outputs=False,
                 multicast_endpoint=None, plugins=None,
                 sockets=None, warmup_delay=0, httpd=False,
                 httpd_host='localhost', httpd_port=8080,
                 httpd_close_outputs=False, debug=False,
                 ssh_server=None, proc_name='circusd', pidfile=None,
                 loglevel=None, logoutput=None, fqdn=None):
        self.watchers = watchers
        self.endpoint = endpoint
        self.check_delay = check_delay
        self.prereload_fn = prereload_fn
        self.pubsub_endpoint = pubsub_endpoint
        self.multicast_endpoint = multicast_endpoint
        self.proc_name = proc_name
        self.ssh_server = ssh_server

        self.pidfile = pidfile
        self.loglevel = loglevel
        self.logoutput = logoutput

        if fqdn is None:
            fqdn = socket.getfqdn()
        self.fqdn = fqdn

        self.ctrl = self.loop = None
        self.socket_event = False

        # initialize zmq context
        self._init_context(context)
        self.pid = os.getpid()
        self._watchers_names = {}
        self.alive = True
        self._lock = RLock()
        self.debug = debug
        if self.debug:
            self.stdout_stream = self.stderr_stream = {'class': 'StdoutStream'}
        else:
            self.stdout_stream = self.stderr_stream = None

        # initializing circusd-stats as a watcher when configured
        self.statsd = statsd
        self.stats_endpoint = stats_endpoint

        self.nodes_directory = {}
        # We add ourselves to the nods directory
        self.nodes_direcotry[self.fqdn] = self.get_endpoint_info()

        if self.statsd:
            cmd = "%s -c 'from circus import stats; stats.main()'" % \
                sys.executable
            cmd += ' --endpoint %s' % self.endpoint
            cmd += ' --pubsub %s' % self.pubsub_endpoint
            cmd += ' --statspoint %s' % self.stats_endpoint
            if ssh_server is not None:
                cmd += ' --ssh %s' % ssh_server
            if debug:
                cmd += ' --log-level DEBUG'
            stats_watcher = Watcher('circusd-stats', cmd, use_sockets=True,
                                    singleton=True,
                                    stdout_stream=self.stdout_stream,
                                    stderr_stream=self.stderr_stream,
                                    copy_env=True, copy_path=True,
                                    close_child_stderr=statsd_close_outputs,
                                    close_child_stdout=statsd_close_outputs)

            self.watchers.append(stats_watcher)

        # adding the httpd
        if httpd:
            cmd = ("%s -c 'from circusweb import circushttpd; "
                   "circushttpd.main()'") % sys.executable
            cmd += ' --endpoint %s' % self.endpoint
            cmd += ' --fd $(circus.sockets.circushttpd)'
            if ssh_server is not None:
                cmd += ' --ssh %s' % ssh_server

            httpd_watcher = Watcher('circushttpd', cmd, use_sockets=True,
                                    singleton=True,
                                    stdout_stream=self.stdout_stream,
                                    stderr_stream=self.stderr_stream,
                                    copy_env=True, copy_path=True,
                                    close_child_stderr=httpd_close_outputs,
                                    close_child_stdout=httpd_close_outputs)

            self.watchers.append(httpd_watcher)
            httpd_socket = CircusSocket(name='circushttpd', host=httpd_host,
                                        port=httpd_port)

            # adding the socket
            if sockets is None:
                sockets = [httpd_socket]
            else:
                sockets.append(httpd_socket)

        # adding each plugin as a watcher
        ch_stderr = self.stderr_stream is None
        ch_stdout = self.stdout_stream is None

        if plugins is not None:
            for plugin in plugins:
                fqn = plugin['use']
                cmd = get_plugin_cmd(plugin, self.endpoint,
                                     self.pubsub_endpoint, self.check_delay,
                                     ssh_server, debug=self.debug)
                plugin_cfg = dict(cmd=cmd, priority=1, singleton=True,
                                  stdout_stream=self.stdout_stream,
                                  stderr_stream=self.stderr_stream,
                                  copy_env=True, copy_path=True,
                                  close_child_stderr=ch_stderr,
                                  close_child_stdout=ch_stdout)
                plugin_cfg.update(plugin)
                if 'name' not in plugin_cfg:
                    plugin_cfg['name'] = fqn

                plugin_watcher = Watcher.load_from_config(plugin_cfg)
                self.watchers.append(plugin_watcher)

        self.sockets = CircusSockets(sockets)
        self.warmup_delay = warmup_delay

    def _init_context(self, context):
        self.context = context or zmq.Context.instance()
        self.loop = ioloop.IOLoop.instance()
        self.ctrl = Controller(self.endpoint, self.multicast_endpoint,
                               self.context, self.loop, self, self.check_delay)

    def get_endpoint_info(self):
        return {'fqdn': socket.getfqdn,
                'pubsub_endpoint': self.pubsub_endpoint,
                'controller_endpoint': self.endpoint,
                'stats_endpoint': self.stats_endpoint}

    def add_new_node(self, data, emitter_addr, send_message):

        data_type = data.get('type')

        if data_type in ('new-node', 'new-node-ack'):
            for node in data.pop('nodes'):
                fqdn = data.pop('fqdn')

                if fqdn != self.fqdn and fqdn not in self.nodes_directory:
                    self.nodes_directory[fqdn] = data

            if data_type == 'new-node':
                send_message(emitter_addr, payload=self.nodes_directory,
                             data_type='new-node-ack')
            print self.nodes_directory

    def get_socket(self, name):
        return self.sockets.get(name, None)

    def get_socket_config(self, config, name):
        for i in config.get('sockets', []):
            if i['name'] == name:
                return i.copy()
        return None

    def get_watcher_config(self, config, name):
        for i in config.get('watchers', []):
            if i['name'] == name:
                return i.copy()
        return None

    def get_plugin_config(self, config, name):
        for i in config.get('plugins', []):
            if i['name'] == name:
                cfg = i.copy()
                cmd = get_plugin_cmd(cfg, self.endpoint,
                                     self.pubsub_endpoint, self.check_delay,
                                     self.ssh_server, debug=self.debug)

                cfg.update(dict(cmd=cmd, priority=1, singleton=True,
                                stdout_stream=self.stdout_stream,
                                stderr_stream=self.stderr_stream,
                                copy_env=True, copy_path=True))
                return cfg
        return None

    @classmethod
    def get_arbiter_config(cls, config):
        cfg = config.copy()
        del cfg['watchers']
        del cfg['plugins']
        del cfg['sockets']

        return cfg

    def reload_from_config(self, config_file=None):
        new_cfg = get_config(config_file if config_file else self.config_file)

        # if arbiter is changed, reload everything
        if self.get_arbiter_config(new_cfg) != self._cfg:
            raise ReloadArbiterException

        # Gather socket names.
        current_sn = set([i.name for i in self.sockets.values()])
        new_sn = set([i['name'] for i in new_cfg.get('sockets', [])])
        added_sn = new_sn - current_sn
        deleted_sn = current_sn - new_sn
        maybechanged_sn = current_sn - deleted_sn
        changed_sn = set([])
        wn_with_changed_socket = set([])
        wn_with_deleted_socket = set([])

        # get changed sockets
        for n in maybechanged_sn:
            s = self.get_socket(n)
            if self.get_socket_config(new_cfg, n) != s._cfg:
                changed_sn.add(n)

                # just delete the socket and add it again
                deleted_sn.add(n)
                added_sn.add(n)

                # Get the watchers whichs use these, so they could be
                # deleted and added also
                for w in self.iter_watchers():
                    if 'circus.sockets.%s' % n.lower() in w.cmd:
                        wn_with_changed_socket.add(w.name)

        # get deleted sockets
        for n in deleted_sn:
            s = self.get_socket(n)
            s.close()
            # Get the watchers whichs use these, these should not be
            # active anymore
            for w in self.iter_watchers():
                if 'circus.sockets.%s' % n.lower() in w.cmd:
                    wn_with_deleted_socket.add(w.name)
            del self.sockets[s.name]

        # get added sockets
        for n in added_sn:
            socket_config = self.get_socket_config(new_cfg, n)
            s = CircusSocket.load_from_config(socket_config)
            s.bind_and_listen()
            self.sockets[s.name] = s

        if added_sn or deleted_sn:
            # make sure all existing watchers get the new sockets in
            # their attributes and get the old removed
            # XXX: is this necessary? self.sockets is an mutable
            # object
            for watcher in self.iter_watchers():
                # XXX: What happens as initalize is called on a
                # running watcher?
                watcher.initialize(self.evpub_socket, self.sockets, self)

        # Gather watcher names.
        current_wn = set([i.name for i in self.iter_watchers()])
        new_wn = set([i['name'] for i in new_cfg.get('watchers', [])])
        new_wn = new_wn | set([i['name'] for i in new_cfg.get('plugins', [])])
        added_wn = (new_wn - current_wn) | wn_with_changed_socket
        deleted_wn = current_wn - new_wn - wn_with_changed_socket
        maybechanged_wn = current_wn - deleted_wn
        changed_wn = set([])

        if wn_with_deleted_socket and wn_with_deleted_socket not in new_wn:
            raise ValueError('Watchers %s uses a socket which is deleted' %
                             wn_with_deleted_socket)

        # get changed watchers
        for n in maybechanged_wn:
            w = self.get_watcher(n)
            new_watcher_cfg = (self.get_watcher_config(new_cfg, n) or
                               self.get_plugin_config(new_cfg, n))
            old_watcher_cfg = w._cfg.copy()
            if new_watcher_cfg != old_watcher_cfg:
                if not w.name.startswith('plugin:'):
                    num_procs = new_watcher_cfg['numprocesses']
                    old_watcher_cfg['numprocesses'] = num_procs
                    if new_watcher_cfg == old_watcher_cfg:
                        # if nothing but the number of processes is
                        # changed, just changes this
                        w.set_numprocesses(int(num_procs))
                        continue

                # Others things are changed. Just delete and add the watcher.
                changed_wn.add(n)
                deleted_wn.add(n)
                added_wn.add(n)

        # delete watchers
        for n in deleted_wn:
            w = self.get_watcher(n)
            w.stop()
            del self._watchers_names[w.name.lower()]
            self.watchers.remove(w)

        # add watchers
        for n in added_wn:
            new_watcher_cfg = (self.get_plugin_config(new_cfg, n) or
                               self.get_watcher_config(new_cfg, n))

            w = Watcher.load_from_config(new_watcher_cfg)
            w.initialize(self.evpub_socket, self.sockets, self)
            self.start_watcher(w)
            self.watchers.append(w)
            self._watchers_names[w.name.lower()] = w

        return False

    @classmethod
    def load_from_config(cls, config_file):
        cfg = get_config(config_file)

        watchers = []
        for watcher in cfg.get('watchers', []):
            watchers.append(Watcher.load_from_config(watcher))

        sockets = []
        for socket in cfg.get('sockets', []):
            sockets.append(CircusSocket.load_from_config(socket))

        httpd = cfg.get('httpd', False)
        if httpd:
            # controlling that we have what it takes to run the web UI
            # if something is missing this will tell the user
            try:
                import circusweb  # NOQA
            except ImportError:
                logger.error('You need to install circus-web')
                sys.exit(1)

        # creating arbiter
        arbiter = cls(watchers, cfg['endpoint'], cfg['pubsub_endpoint'],
                      check_delay=cfg.get('check_delay', 1.),
                      prereload_fn=cfg.get('prereload_fn'),
                      statsd=cfg.get('statsd', False),
                      stats_endpoint=cfg.get('stats_endpoint'),
                      multicast_endpoint=cfg.get('multicast_endpoint'),
                      plugins=cfg.get('plugins'), sockets=sockets,
                      warmup_delay=cfg.get('warmup_delay', 0),
                      httpd=httpd,
                      httpd_host=cfg.get('httpd_host', 'localhost'),
                      httpd_port=cfg.get('httpd_port', 8080),
                      debug=cfg.get('debug', False),
                      ssh_server=cfg.get('ssh_server', None),
                      pidfile=cfg.get('pidfile', None),
                      loglevel=cfg.get('loglevel', None),
                      logoutput=cfg.get('logoutput', None),
                      fqdn=cfg.get('fqdn', None))

        # store the cfg which will be used, so it can be used later
        # for checking if the cfg has been changed
        arbiter._cfg = cls.get_arbiter_config(cfg)
        arbiter.config_file = config_file

        return arbiter

    def iter_watchers(self, reverse=True):
        watchers = [(watcher.priority, watcher) for watcher in self.watchers]
        watchers.sort(reverse=reverse)
        for __, watcher in watchers:
            yield watcher

    @debuglog
    def initialize(self):
        # set process title
        _setproctitle(self.proc_name)

        # event pub socket
        self.evpub_socket = self.context.socket(zmq.PUB)
        self.evpub_socket.bind(self.pubsub_endpoint)
        self.evpub_socket.linger = 0

        # initialize sockets
        if len(self.sockets) > 0:
            self.sockets.bind_and_listen_all()
            logger.info("sockets started")

        # initialize watchers
        for watcher in self.iter_watchers():
            self._watchers_names[watcher.name.lower()] = watcher
            watcher.initialize(self.evpub_socket, self.sockets, self)

    def start_watcher(self, watcher):
        """Aska a specific watcher to start and wait for the specified
        warmup delay."""
        if watcher.autostart:
            watcher.start()
            sleep(self.warmup_delay)

    @debuglog
    def start(self):
        """Starts all the watchers.

        The start command is an infinite loop that waits
        for any command from a client and that watches all the
        processes and restarts them if needed.
        """
        logger.info("Starting master on pid %s", self.pid)
        self.initialize()

        # start controller
        self.ctrl.start()
        try:
            # initialize processes
            logger.debug('Initializing watchers')
            for watcher in self.iter_watchers():
                self.start_watcher(watcher)

            logger.info('Arbiter now waiting for commands')

            while True:
                try:
                    self.loop.start()
                except zmq.ZMQError as e:
                    if e.errno == errno.EINTR:
                        continue
                    else:
                        raise
                else:
                    break
        finally:
            self.ctrl.stop()
            self.evpub_socket.close()
            if len(self.sockets) > 0:
                self.sockets.close_all()

    def stop(self):
        self.stop_watchers(stop_alive=True)
        # this will stop the loop and the closing
        # will finish in .start()
        self.loop.stop()

    def reap_processes(self):
        # map watcher to pids
        watchers_pids = {}
        for watcher in self.iter_watchers():
            if not watcher.stopped:
                for process in watcher.processes.values():
                    watchers_pids[process.pid] = watcher

        # detect dead children
        while True:
            try:
                # wait for our child (so it's not a zombie)
                pid, status = os.waitpid(-1, os.WNOHANG)
                if not pid:
                    break

                if pid in watchers_pids:
                    watcher = watchers_pids[pid]
                    watcher.reap_process(pid, status)
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    sleep(0)
                    continue
                elif e.errno == errno.ECHILD:
                    # process already reaped
                    return
                else:
                    raise

    def manage_watchers(self):
        if not self.alive:
            return

        with self._lock:
            need_on_demand = False
            # manage and reap processes
            self.reap_processes()
            for watcher in self.iter_watchers():
                if watcher.on_demand and watcher.stopped:
                    need_on_demand = True
                watcher.manage_processes()

            if need_on_demand:
                sockets = [x.fileno() for x in self.sockets.values()]
                rlist, wlist, xlist = select.select(sockets, [], [], 0)
                if rlist:
                    self.socket_event = True
                    self.start_watchers()
                    self.socket_event = False

    @debuglog
    def reload(self, graceful=True):
        """Reloads everything.

        Run the :func:`prereload_fn` callable if any, then gracefuly
        reload all watchers.
        """
        if self.prereload_fn is not None:
            self.prereload_fn(self)

        # reopen log files
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.acquire()
                handler.stream.close()
                handler.stream = open(handler.baseFilename, handler.mode)
                handler.release()

        # gracefully reload watchers
        for watcher in self.iter_watchers():
            watcher.reload(graceful=graceful)
            sleep(self.warmup_delay)

    def numprocesses(self):
        """Return the number of processes running across all watchers."""
        return sum([len(watcher) for watcher in self.watchers])

    def numwatchers(self):
        """Return the number of watchers."""
        return len(self.watchers)

    def get_watcher(self, name):
        """Return the watcher *name*."""
        return self._watchers_names[name]

    def statuses(self):
        return dict([(watcher.name, watcher.status())
                     for watcher in self.watchers])

    def add_watcher(self, name, cmd, **kw):
        """Adds a watcher.

        Options:

        - **name**: name of the watcher to add
        - **cmd**: command to run.
        - all other options defined in the Watcher constructor.
        """
        if name in self._watchers_names:
            raise AlreadyExist("%r already exist" % name)

        if not name:
            return ValueError("command name shouldn't be empty")

        watcher = Watcher(name, cmd, **kw)
        watcher.initialize(self.evpub_socket, self.sockets, self)
        self.watchers.append(watcher)
        self._watchers_names[watcher.name.lower()] = watcher
        return watcher

    def rm_watcher(self, name):
        """Deletes a watcher.

        Options:

        - **name**: name of the watcher to delete
        """
        logger.debug('Deleting %r watcher', name)

        # remove the watcher from the list
        watcher = self._watchers_names.pop(name)
        del self.watchers[self.watchers.index(watcher)]

        # stop the watcher
        watcher.stop()

    def start_watchers(self):
        for watcher in self.iter_watchers():
            watcher.start()
            sleep(self.warmup_delay)

    def stop_watchers(self, stop_alive=False):
        if not self.alive:
            return

        if stop_alive:
            logger.info('Arbiter exiting')
            self.alive = False

        for watcher in self.iter_watchers(reverse=False):
            watcher.stop()

    def restart(self):
        self.stop_watchers()
        self.start_watchers()


class ThreadedArbiter(Arbiter, Thread):

    def __init__(self, *args, **kw):
        Thread.__init__(self)
        Arbiter.__init__(self, *args, **kw)

    def start(self):
        return Thread.start(self)

    def run(self):
        return Arbiter.start(self)

    def stop(self):
        Arbiter.stop(self)
        if get_ident() != self.ident and self.isAlive():
            self.join()
