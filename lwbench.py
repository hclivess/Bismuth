import connections
import collections
import socks
import time

DEFAULT_PORT = 5658


def convert_ip_port(ip, some_port):
    """
    Get ip and port, but extract port from ip if ip was as ip:port
    :param ip:
    :param some_port: default port
    :return: (ip, port)
    """
    if ':' in ip:
        ip, some_port = ip.split(':')
    return ip, some_port


def connectible(ipport):
    """return True if the ip:port can be connected to, without sending any command"""
    try:
        s = socks.socksocket()
        s.settimeout(3)
        ip, local_port = convert_ip_port(ipport, DEFAULT_PORT)
        s.connect((ip, int(local_port)))
        return True
    except:
        return False


def time_measure(light_ip, app_log):
    port = DEFAULT_PORT
    result_collection = {ip: [0, 0] for ip in light_ip}

    for address in result_collection:
        try:
            ip, local_port = convert_ip_port(address, port)
            print("Attempting to benchmark {}:{}".format(ip, local_port))
            s = socks.socksocket()
            s.settimeout(3)

            if local_port == DEFAULT_PORT: #doesn't work if a node uses non standard port, bench in else-path - will fail
                #start benchmark
                timer_start = time.time()
                s.connect((ip, int(local_port)))
                connections.send(s, "statusget", 10)
                result = connections.receive(s, 10, 5)
                timer_result = (time.time() - timer_start) * 5 #penalty to prio Wallet-Servers before nodes. local node should be so fast, to be still fastest, else it is better that a wallet-server is chosen!
                result_collection[address] = timer_result, result[8][7]
                app_log.warning("Result for {}:{}, a normal node, penalty-factor *5 (real result time/5): {}".format(ip, local_port, timer_result))
                #finish benchmark
            else:
                #start benchmark
                timer_start = time.time()
                s.connect((ip, int(local_port)))
                connections.send(s, "statusget", 10)
                result = connections.receive(s, 10, 5)
                connections.send(s, "wstatusget", 10)
                result_ws = connections.receive(s, 10, 5)
                timer_result = time.time() - timer_start
                #finish benchmark and load balance if too many clients
                ws_clients = result_ws.get('clients')
                if ws_clients > 300:
                    timer_result = timer_result + ws_clients/1000
                    app_log.warning("Result for {}:{}, modified due to high client load: {}".format(ip, local_port, timer_result))
                elif ws_clients > 150:
                    timer_result = timer_result + ws_clients/10000
                    app_log.warning("Result for {}:{}, modified due to client load: {}".format(ip, local_port, timer_result))
                else:
                    app_log.warning("Result for {}:{}, low load - unmodified: {}".format(ip, local_port, timer_result))
                result_collection[address] = timer_result, result[8][7]

        except Exception as e:
            print("Cannot benchmark {}:{}".format(ip, local_port))

    # sort IPs for measured Time
    bench_result = collections.OrderedDict(sorted((value[0], key) for (key, value) in result_collection.items()))
    light_ip = list(bench_result.values())

    max_height_temp = list(result_collection.values())
    max_height = max(list(zip(*max_height_temp))[1])
    for key, value in result_collection.items():
        if int(value[1]) < (max_height - 5):
            try:
                light_ip.remove(key)
                light_ip.append(key)
            except Exception as e:
                pass
    return light_ip
