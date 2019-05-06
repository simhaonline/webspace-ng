from urllib import parse
from functools import wraps
import ipaddress
from queue import Queue
import json
import logging
import grp
import stat
import os
import time
from os import path
import shutil
import threading
import select
import socket

from eventfd import EventFD
from pylxd import Client
from pylxd.models import Operation
from ws4py.client import WebSocketBaseClient
from ws4py.messaging import TextMessage

from .. import ADMIN_GROUP, WebspaceError

def str2bool(s):
    ls = s.lower()
    if ls == 'true':
        return True
    if ls == 'false':
        return False
    raise ValueError('Invalid boolean value {}'.format(s))

def image_info(image):
    return {
        'fingerprint': image.fingerprint,
        'aliases': image.aliases,
        'properties': image.properties,
        'size': image.size,
    }

class ConsoleControl(WebSocketBaseClient):
    def __init__(self, ws_uri, resource, *args, **kwargs):
        WebSocketBaseClient.__init__(self, ws_uri, *args, **kwargs)
        self.resource = resource

    def resize(self, width, height):
        payload = json.dumps({
            'command': 'window-resize',
            'args': {
                'width': width,
                'height': height
            }
        }).encode('utf-8')

        self.send(payload, binary=False)
    def received_message(self, message):
        print('control msg', message.data)
class ConsoleSession(WebSocketBaseClient):
    def __init__(self, user, ws_uri, console_path, control_path, *args, **kwargs):
        self.__shutdown_event = EventFD()

        self.socket_path = path.join('/tmp', '{}-ws-console.socket'.format(user))
        try:
            os.unlink(self.socket_path)
        except OSError:
            if os.path.exists(self.socket_path):
                raise

        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(self.socket_path)
        self.socket.listen(1)
        shutil.chown(self.socket_path, user=user)
        os.chmod(self.socket_path, stat.S_IRWXU)

        self.control = ConsoleControl(ws_uri, control_path)
        self.control.connect()

        WebSocketBaseClient.__init__(self, ws_uri, *args, **kwargs)
        self.resource = console_path
        self.socket_conn = None
        self.run_thread = threading.Thread(target=self.run_read)

        self.connect()

    def __accept(self):
        while True:
            r, _, _ = select.select([self.__shutdown_event, self.socket], [], [])
            if self.__shutdown_event in r:
                break
            if self.socket in r:
                self.socket_conn, _ = self.socket.accept()
                break
    def __read_loop(self):
        while True:
            r, _, _ = select.select([self.__shutdown_event, self.sock, self.control.sock, self.socket_conn], [], [])
            if self.__shutdown_event in r:
                break
            if self.sock in r:
                if not self.once():
                    logging.debug('websocket error')
                    break
            if self.control.sock in r:
                if not self.control.once():
                    logging.debug('control websocket error')
                    break
            if self.socket_conn in r:
                try:
                    read = self.socket_conn.recv(4096)
                except:
                    logging.debug('pipe socket error')
                    break
                if not read:
                    # Socket was closed
                    break

                self.send(read, binary=True)
    def run_read(self):
        self.__accept()
        self.socket.close()

        if self.socket_conn is not None:
            self.__read_loop()
            self.socket_conn.close()

        os.unlink(self.socket_path)

        logging.debug('closing websockets')
        try:
            self.control.close()
            self.close()
        except:
            pass
        self.control.terminate()
        self.terminate()

    def start(self):
        self.run_thread.start()
    def join(self):
        self.run_thread.join()
    def stop(self, join=False):
        self.__shutdown_event.set()
        if join:
            self.join()

    def received_message(self, message):
        # Apparently a text message is a "message barrier"
        if isinstance(message, TextMessage):
            logging.debug('received websocket message barrier')
            self.stop()
            return

        if self.socket_conn is not None:
            self.socket_conn.sendall(message.data)

def check_user(f):
    @wraps(f)
    def wrapper(self, *args):
        req = self.server.current_request
        if req.client_user in self.admins:
            return f(self, *args)
        return f(self, req.client_user, *args)
    return wrapper
def check_admin(f):
    @wraps(f)
    def wrapper(self, *args):
        req = self.server.current_request
        if req.client_user not in self.admins:
            raise WebspaceError('You must be an admin to call this function')
        return f(self, *args)
    return wrapper
def check_init(f):
    @wraps(f)
    @check_user
    def wrapper(self, user, *args):
        container_name = self.user_container(user)
        if not self.client.containers.exists(container_name):
            raise WebspaceError('Your container has not been initialized')
        container = self.client.containers.get(container_name)
        return f(self, user, container, *args)
    return wrapper
def check_running(f):
    @wraps(f)
    @check_init
    def wrapper(self, user, container, *args):
        if container.status_code != 103:
            raise WebspaceError('Your container is not running')
        return f(self, user, container, *args)
    return wrapper
def check_console(f):
    @wraps(f)
    @check_running
    def wrapper(self, user, container, *args):
        if not user in self.console_sessions:
            raise WebspaceError("Your container doesn't have an active console session")
        session = self.console_sessions[user]
        return f(self, user, container, session, *args)
    return wrapper

class Manager:
    allowed = {'images', 'init', 'status', 'log', 'console', 'console_close',
               'console_resize', 'shutdown', 'reboot', 'delete', 'boot_and_host',
               'get_config', 'set_option', 'unset_option'}

    def __init__(self, config, server):
        self.config = config

        endpoint = 'http+unix://{}'.format(parse.quote(config.lxd.socket, safe=''))
        self.client = Client(endpoint=endpoint)
        self.server = server
        self.admins = set(grp.getgrnam(ADMIN_GROUP).gr_mem)
        self.console_sessions = {}
        self.reserved_options = {
            'terminate_ssl': str2bool,
            'startup_delay': self.startup_delay
        }

        self.running_containers = list(map(lambda c: c.name, filter(
            lambda c: c.name.endswith(self.config.lxd.suffix) and c.status_code == 103,
            self.client.containers.all())))
        self.container_lock = threading.RLock()

        logging.debug('containers running at startup: %s', self.running_containers)

    def _stop(self):
        for session in self.console_sessions.values():
            session.stop(join=True)

        with self.container_lock:
            for c in self.running_containers:
                container = self.client.containers.get(c)
                if container.status_code == 103:
                    container.stop(wait=True)

    def user_container(self, user):
        return '{}{}'.format(user, self.config.lxd.suffix)
    def get_new_config(self, user, image):
        return {
            'name': self.user_container(user),
            'ephemeral': False,
            'profiles': [self.config.lxd.profile],
            'source': {
                'type': 'image',
                'fingerprint': image
            },
            'config': {
                'user.terminate_ssl': self.config.defaults.terminate_ssl,
                'user.startup_delay': self.config.defaults.startup_delay
            }
        }
    def startup_delay(self, i):
        i = int(i)
        if i > self.config.max_startup_delay:
            raise ValueError('Startup delay is too large (max {})'.format(self.config.max_startup_delay))
        if i < 0:
            raise ValueError('Startup delay must be positive')
        return i
    def start_container(self, container):
        with self.container_lock:
            if len(self.running_containers) == self.config.run_limit:
                c = self.running_containers.pop(0)
                to_shutdown = self.client.containers.get(c)
                if to_shutdown.status_code == 103:
                    logging.debug('at run limit, shutting down container %s', to_shutdown.name)
                    to_shutdown.stop(wait=True)

            logging.info('booting container %s', container.name)
            container.start(wait=True)
            self.running_containers.append(container.name)
            # Wait for the container to get an IP
            time.sleep(self.get_user_option(container, 'startup_delay'))

    @check_user
    def images(self, _):
        return list(map(image_info, self.client.images.all()))

    @check_user
    def init(self, user, image_fingerprint):
        container_name = self.user_container(user)
        if self.client.containers.exists(container_name):
            raise WebspaceError('Your container has already been initialized!')

        self.client.containers.create(self.get_new_config(user, image_fingerprint), wait=True)

    @check_init
    def status(self, _, container):
        return container.state()

    @check_running
    def log(self, _user, container):
        response = container.api['console'].get()
        return response.text

    @check_init
    def console(self, user, container, t_width, t_height):
        if container.status_code != 103:
            self.start_container(container)

        response = container.api['console'].post(json={
            'width': t_width,
            'height': t_height
        }).json()

        # Get the control websocket path
        operation_id = Operation.extract_operation_id(response['operation'])
        ws_uri = self.client.api.operations[operation_id] \
                 .websocket._api_endpoint
        ws_path = parse.urlparse(ws_uri).path

        # Get the secrets for the console fd and control fd
        fds = response['metadata']['metadata']['fds']

        console_path = '{}?secret={}'.format(ws_path, fds['0'])
        control_path = '{}?secret={}'.format(ws_path, fds['control'])

        if user in self.console_sessions:
            logging.info('closing existing console session for %s', user)
            self.console_sessions[user].stop(join=True)
        session = ConsoleSession(user, self.client.websocket_url, console_path, control_path)
        session.start()
        self.console_sessions[user] = session

        return session.socket_path

    @check_console
    def console_resize(self, _user, _container, session, t_width, t_height):
        session.control.resize(t_width, t_height)

    @check_console
    def console_close(self, user, _, session):
        session.stop(join=True)
        del self.console_sessions[user]

    @check_running
    def shutdown(self, _user, container):
        with self.container_lock:
            container.stop(wait=True)
            self.running_containers.remove(container.name)

    @check_running
    def reboot(self, _user, container):
        container.restart(wait=True)

    @check_init
    def delete(self, _user, container):
        if container.status_code == 103:
            container.stop(wait=True)
        container.delete(wait=True)

    @check_init
    def get_config(self, _user, container):
        return {k[len('user.'):]: v for k, v in container.config.items() if k.startswith('user.')}

    @check_init
    def set_option(self, _user, container, key, value):
        if key in self.reserved_options:
            # Validate the input before setting
            self.reserved_options[key](value)

        container.config['user.{}'.format(key)] = value
        container.save()

    @check_init
    def unset_option(self, _user, container, key):
        if key in self.reserved_options:
            raise WebspaceError('{} is a reserved option and may not be unset'.format(key))

        del container.config['user.{}'.format(key)]
        container.save()

    def get_user_option(self, container, key):
        value = container.config['user.{}'.format(key)]
        if key in self.reserved_options:
            return self.reserved_options[key](value)
        return value

    @check_admin
    def boot_and_host(self, user, https_hint):
        container_name = self.user_container(user)
        if not self.client.containers.exists(container_name):
            return None, 'init'

        container = self.client.containers.get(container_name)
        if container.status_code != 103:
            self.start_container(container)

        info = container.state()
        if self.config.lxd.net.container_iface not in info.network:
            return None, 'iface'
        for ip in map(
                      lambda i: ipaddress.IPv4Address(i['address']),
                      filter(
                             lambda i: i['family'] == 'inet',
                             info.network[self.config.lxd.net.container_iface]['addresses'])):
            if ip in self.config.lxd.net.cidr:
                scheme = 'https' if https_hint and not self.get_user_option(container, 'terminate_ssl') else 'http'
                return scheme, str(ip)

        return None, 'ip'

    def _dispatch(self, method, params):
        if not method in Manager.allowed:
            raise Exception('method "{}" is not supported'.format(method))

        try:
            return getattr(self, method)(*params)
        except:
            import traceback
            traceback.print_exc()
            raise
