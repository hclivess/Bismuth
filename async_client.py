"""
Async TCP Client for Bismuth Wallet

Eggdrasyl
Bismuth Foundation

July 2018

requires 
pip3 install tornado
"""


from tornado.tcpclient import TCPClient
import asyncio
import json
import time
import random


__version__ = '0.0.31'


class AsyncClient():


    def __init__(self, server_list, app_log, loop, rebench_timer, address=''):
        # print("async init", server_list)
        self.server_list = server_list
        self.app_log = app_log
        self.loop = loop
        self.address = address
        self.connected = False
        self.stream = None
        self._status = {"connected": False, "address": self.address}
        self.last_full_refresh = 0
        self.refreshing = False
        self.cached_aliases = {}
        self.needed_aliases = []
        self.ip_port = 'N/A'
        self.rebench_timer = rebench_timer
        self.block_height = 0
        self.block_height_old = 0
        self.block_timestamp = time.time()

    def status(self, address):
        self.address = address
        return self._status
		
    def loop_status(self):
        return self.loop.is_running()
		
    async def refresh(self):
        if not self.stream:
            return
        if self.refreshing:
            return
        try:  
            self.refreshing = True
            statusget = await self._command("statusget")
            self._status['statusget'] = statusget
            self._status['status_version'] = statusget[7]
            self._status['stats_timestamp'] = statusget[9]
            self.block_height = statusget[8][7]
            if self.block_height != self.block_height_old:
                self.block_height_old = self.block_height
                self.block_timestamp = time.time()

            #rebench servers if last benchmark too old
            if (self.rebench_timer + 7200.0) < time.time():
                self.loop.stop()
            #rebench if too far behind    
            elif (time.time() > self.block_timestamp + 300):
                self.loop.stop()
            #    pass
            else:
                pass
            
            			
            # The two following ones only, depend on the address
            if self.address:
                stats_account = await self._command("balanceget", self.address)
                self._status['stats_account'] = stats_account
                self._status['address'] = self.address

                await self._send("addlistlim")
                await self._send(self.address)
                await self._send("20")
                self._status['addlist'] = await self._receive()

            self._status['block_get'] = await self._command("blocklast")
            self._status['diffget'] = await self._command("diffget")
            # Maybe not suitable to ask every 10 sec
            self._status['mpget'] = await self._command("mpget")

            if self.last_full_refresh < time.time() - 60 * 5:
                # Only run this if the info we got is older then 5 min old
                self._status['annverget'] = await self._command("annverget")
                self._status['annget'] = await self._command("annget")
                self.last_full_refresh = time.time()

            if len(self.needed_aliases):
                # Do we have aliases to resolve?
                solved = await self._command("aliasesget", self.needed_aliases)
                for index, alias in enumerate(self.needed_aliases):
                    self.cached_aliases[self.needed_aliases[index]] = solved[index]
                self.needed_aliases = []


        except Exception as e:
            self.app_log.error('refresh: {}'.format(e))
        finally:
            self.refreshing = False

    def aliases(self, needed_aliases):
        """
        cached_aliases hold the one we resolved already
        needed hold the one we, well, need
        This function has to return right away, can't wait for the results but will trigger anyway.
        :param needed_aliases:
        :return:
        """
        results = {}
        # willingly not using list/dict comprehensions to be easier to understand
        for alias in needed_aliases:
            if alias in self.cached_aliases:
                results[alias] = self.cached_aliases[alias]
            else:
                # display ... to say we are looking for it
                results[alias] = '[...] ' + alias
                if not alias in self.needed_aliases:
                    #  make sure they are unique
                    self.needed_aliases.append(alias)
        # they will be fetched in the background
        if len(self.needed_aliases):
            # force a refresh right away if we need aliases
            asyncio.run_coroutine_threadsafe(self.refresh(), self.loop)
        return results

    async def _receive(self):
        """
        Get a command, async version
        :param stream:
        :param ip:
        :return:
        """
        if self.stream:
            header = await self.stream.read_bytes(10)
            data_len = int(header)
            data = await self.stream.read_bytes(data_len)
            data = json.loads(data.decode("utf-8"))
            return data
        else:
            self.app_log.warning('receive: not connected')

    def receive(self, timeout=None):
        future = asyncio.run_coroutine_threadsafe(self._receive(), self.loop)
        return future.result(timeout)

    def send(self, data, timeout=None):
        future = asyncio.run_coroutine_threadsafe(self._send(data), self.loop)
        return future.result(timeout)

    def command(self, data,  param=None, timeout=None):
        future = asyncio.run_coroutine_threadsafe(self._command(data, param), self.loop)
        return future.result(timeout)

    async def _send(self, data):
        """
        sends an object to the stream, async.
        :param data:
        :param stream:
        :param ip:
        :return:
        """
        if self.stream:
            try:
                data = str(json.dumps(data))
                header = str(len(data)).encode("utf-8").zfill(10)
                full = header + data.encode('utf-8')
                await self.stream.write(full)
            except Exception as e:
                self.app_log.error("send_to_stream {} for ip {}".format(str(e), self.ip_port))
                self.stream = None
                self.connected = False
                raise
        else:
            self.app_log.warning('send: not connected')

    async def _command(self, data, param=None):
        if self.stream:
            await self._send(data)
            if param:
                await self._send(param)
            return await self._receive()
        else:
            self.app_log.warning('command: not connected')
            return None

    def convert_ip_port(self, ip, some_port):
        """
        Get ip and port, but extract port from ip if ip was as ip:port
        :param ip:
        :param some_port: default port
        :return: (ip, port)
        """
        if ':' in ip:
            ip, some_port = ip.split(':')
        return ip, some_port

    async def background(self):
        """
        background task that tries to stay connected all the time
        :return:
        """
        try:
            while True:
                for self.ip_port in self.server_list:
                    self.app_log.warning("async client trying to connect to {}".format(self.ip_port))
                    try:
                        ip, port = self.convert_ip_port(self.ip_port, 5658)
                        self.stream = await TCPClient().connect(ip, port)
                        if self.stream:
                            self.connected = True
                            self.app_log.warning("connected to {}".format(self.ip_port))
                        while self.stream:
                            await self.refresh()
                            await asyncio.sleep(10)
                    except Exception as e:
                        self.app_log.error("Error in Stream: {} for {}".format(e, self.ip_port))
                    finally:
                        if self.stream:
                            self.stream.close()
                            self.stream = None
                        self.connected = False
                    await asyncio.sleep(5)
        except Exception as e:
            self.app_log.error("Error background {} for {}".format(e, self.ip_port))

connection = None

if __name__ == "__main__":
    print("I'm a module, can't run")
