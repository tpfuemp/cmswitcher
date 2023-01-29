#!/usr/bin/env python3
import subprocess
import requests
import socket
import json
import time
from tabulate import tabulate
import argparse
import os

# Read config

try:
    config, miners, pools, algos = [(json.load(open(f"data/{file}.json")) for file in ["config", "miners", "pools", "algos"])]
except FileNotFoundError:
    print("Missing data files.")
    exit()

# Inits
mbtc_value = 0

parser = argparse.ArgumentParser()
parser.add_argument('--cpuminer', help='cpuminer binary location', default='cpuminer')
args = parser.parse_args()

# Check if cpuminer is available
if not os.path.isfile(args.cpuminer):
    print("cpuminer not found at %s" % args.cpuminer)
    exit()

def get_api_data():
    resp = ""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", 40101))
    s.sendall(b'summary')
    while not '|' in resp:
        resp += s.recv(32).decode('utf-8')
    s.close()
    return {k: v for k, v in (x.split("=") for x in resp.split(";"))}


def find_pool_algo_name(pool, algo):
    result = None
    if algo in pools[pool]["results"].keys():
        result = algo
    else:
        for entry in pools[pool]["results"].keys():
            if entry.lower() == algo.lower():
                result = entry
    if result is None:
        for variation in algos:
            if algo in variation:
                for entry in variation:
                    if entry in pools[pool]["results"].keys():
                        result = entry
    return result


def pool_find_supported_algo(pool):
    print("Probing pool %s..." % pool, end="")
    pools[pool]["results"] = requests.get(pools[pool]["api"]).json()
    match = list(pools[pool]["results"].keys())
    print("%s algos supported" % len(match))
    return match


def find_common_algos(list1, list2):
    results = {}
    for item1 in list1:
        if item1 in list2:
            results.update({item1: item1})
        else:
            for item2 in list2:
                if item1.lower() == item2.lower():
                    results.update({item1: item2})
                else:
                    for entry in algos:
                        #print (entry + " " + item1 + " " + item2)
                        if (item1 == entry) and (item2 == entry):
                            print("Adding '%s' as a name variation of '%s'" %
                                  (item1, item2))
                            results.update({item1: item2})

    return results


def populate_supported_algos():
    for miner in miners.keys():
        print("Probing miner %s..." % miner, end="")
        miners[miner]["supported_algos"] = miners[miner]["std_algos"] + \
            list(miners[miner]["custom_algos"].keys())
        print("%s algos supported" % len(miners[miner]["supported_algos"]))
        print(miners[miner]["supported_algos"])

    for pool in pools.keys():
        pools[pool]["supported_algos"] = pool_find_supported_algo(pool)
        print(pools[pool]["supported_algos"])


def benchmark(miner, algo, pool, pool_params):
    if algo in miners[miner]["std_algos"]:
        launch_params = ["-a", algo]
    else:
        launch_params = []
        for k, v in miners[miner]["custom_algos"][algo].items():
            launch_params.append(str(k))
            launch_params.append(str(v))

    if isinstance(pool_params, dict):
        print("Online benchmark for %s - %s on %s" %
              (miner, algo, pool_params["url"]))
        cmdline = [args.cpuminer]
        cmdline += launch_params + \
            miners[miner]["launch_pattern"].format(**pool_params).split(" ")
        print(" ".join(cmdline))

        proc = subprocess.Popen(cmdline,
                                stdout=subprocess.PIPE)
    else:
        print("Offline benchmark for %s - %s" % (miner, algo))
        proc = subprocess.Popen([miner, '-a', algo] + miners[miner]
                                ["offline_bench"].split(" "), stdout=subprocess.PIPE)

    # print("Launched pid %s" % proc.pid)

    try:
        outs, errs = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass

    if proc.returncode is not None:
        print("Miner crashed!! - Unsupported algo?")
        #print(str(outs), str(errs))
        return 0
    else:
        pool_algo = find_pool_algo_name(pool, algo)
        max_hashrate = 0
        accepted_shares = 0
        revenue = 0
        rejected_shares = 0
        t_end = time.time() + config["benchmark_period"]
        t_give_up = time.time() + config["give_up_benchmark_low_profit_secs"]
        while time.time() < t_end and \
                accepted_shares < config["complete_benchmark_min_shares"] and \
                (time.time() < t_give_up or revenue > config["min_profit"]) and \
                rejected_shares < config["max_rejected_shares"]:
            ret = get_api_data()
            if "HS" in ret.keys():
                hashrate = int(float(ret["HS"]))
            elif "KHS" in ret.keys():
                hashrate = int(float(ret["KHS"]) * 1000)
            accepted_shares = int(ret["ACC"])
            rejected_shares = int(ret["REJ"])
            if hashrate > max_hashrate:
                max_hashrate = hashrate
            if hashrate > 0:
                revenue = calc_pool_profitability(pool, pool_algo, hashrate)
            print(
                "[%s %s](%ss) Curr Profitability: USD %.4f Shares: %sA/%sR - Hashrate: %s/Max: %s                                \r" % (
                    miner, algo, (int(t_end - time.time()) if revenue > config["min_profit"] else int(
                        t_give_up - time.time())), revenue, accepted_shares, int(ret["REJ"]), hashrate,
                    max_hashrate), end="")
            time.sleep(1)
        proc.kill()
        print("[FINISHED]: Using hashrate %s for %s (%s accepted shares)                                                           " % (
            hashrate, algo, accepted_shares))
        use_rate = hashrate
        if accepted_shares == 0:
            print("[WARNING]: No accepted shares!")

    return use_rate


def fetch_mbitcoin_value():
    global mbtc_value
    if mbtc_value == 0:
        mbtc_value = requests.get(
            "https://api.coindesk.com/v1/bpi/currentprice.json").json()
    return float(mbtc_value['bpi']['USD']['rate'].replace(",", "")) / 1000


def run_all_benchmarks(skip_existing):
    for miner in miners:
        try:
            miners[miner]["benchmark"] = json.load(
                open('benchmark-%s.json' % miner))
            print("Reading existing benchmark-%s.json" % miner)
        except:
            print("File benchmark-%s.json does not exist, creating a new one." % miner)
            miners[miner]["benchmark"] = {}

        for pool in pools:
            common_algos = find_common_algos(
                miners[miner]["supported_algos"], pools[pool]["supported_algos"])
            print("Miner %s and pool %s have %s algos in common" %
                  (miner, pool, len(common_algos.keys())))
            for algo in common_algos.keys():
                if (algo not in miners[miner][
                        "benchmark"].keys() or not skip_existing) and algo not in config["blacklisted_algos"]:
                    # Launch bench here
                    pool_params = {"algo": algo,
                                   "wallet": pools[pool]["wallet"],
                                   "password": pools[pool]["password"],
                                   "url": pools[pool]["mine_url"].format(algo=common_algos[algo]),
                                   "port": pools[pool]["results"][common_algos[algo]]["port"]}
                    hashrate = benchmark(miner, algo, pool, pool_params)
                    miners[miner]["benchmark"][algo] = hashrate
                    json.dump(miners[miner]["benchmark"], open("benchmark-%s.json" % miner, 'w'),
                              sort_keys=True, indent=4, separators=(',', ': '))
                    print("Updated benchmark-%s.json !" % miner)


def calc_pool_profitability(pool, algo, hashrate):
    mbtc = fetch_mbitcoin_value()
    revenues = {}
    if algo in pools[pool]["results"].keys():
        fields = ['estimate_current', 'estimate_last24h']
        for field in fields:
            revenues[field] = (float(pools[pool]["results"][algo][field])*1000) * (
                (float(hashrate) / 1000000) / float(pools[pool]["results"][algo]["mbtc_mh_factor"])) * mbtc

        fields = ['actual_last24h']
        for field in fields:
            revenues[field] = float(pools[pool]["results"][algo][field]) * (
                (float(hashrate) / 1000000) / float(pools[pool]["results"][algo]["mbtc_mh_factor"])) * mbtc
    else:
        revenues = {'estimate_current': 0}
    return float(revenues['estimate_current'])


def get_current_profit_table():
    profit_table = []
    for miner in miners:
        for pool in pools:
            for algo in miners[miner]["benchmark"].keys():
                pool_algo = find_pool_algo_name(pool, algo)
                value = calc_pool_profitability(
                    pool, pool_algo, miners[miner]["benchmark"][algo])
                if value > config["min_profit"]:
                    profit_table.append(
                        [miner, pool, algo, "{0:.5f}".format(value)])
    return sorted(profit_table, key=lambda x: x[3], reverse=True)


if __name__ == "__main__":
    populate_supported_algos()
    benchmarked_algos = run_all_benchmarks(True)
    print(tabulate(get_current_profit_table()))
