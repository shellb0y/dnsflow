#!/usr/bin/env python

'''
See dnsflow.c header comment for packet formats.
'''

import sys, time, argparse
import socket
import dpkt, pcap
import struct
import ipaddr

DNSFLOW_FLAG_STATS = 0x0001
DEFAULT_PCAP_FILTER = 'udp and dst port 5300'

# Utility functions to simplify interface.
# E.g.
# for dflow in flow_iter(interface='eth0'):
#     print dflow
# for dflow in flow_iter(pcap_file='dnsflow.pcap'):
#     print dflow
def flow_iter(**kwargs):
    rdr = reader(**kwargs)
    return rdr.flow_iter()
def pkt_iter(**kwargs):
    rdr = reader(**kwargs)
    return rdr.pkt_iter()

# Top-level interface for reading/capturing dnsflow. Instantiate object,
# then iterate using flow_iter() or pkt_iter().
class reader(object):
    def __init__(self, interface=None, pcap_file=None,
            pcap_filter=DEFAULT_PCAP_FILTER, stats_only=False):
        if interface is None and pcap_file is None:
            raise Exception('Specify interface or pcap_file')
        if interface is not None and pcap_file is not None:
            raise Exception('Specify only interface or pcap_file')

        self.interface = interface
        self.pcap_file = pcap_file
        self.pcap_filter = pcap_filter
        self.stats_only = stats_only

        self._pcap = pcap.pcapObject()

        if self.pcap_file is not None:
            # XXX dpkt pcap doesn't support filters and there's no way to pass
            # a gzip fd to pylibpcap. Bummer.
            self._pcap.open_offline(pcap_file)
        else:
            # Interface
            # device, snaplen, promisc, to_ms
            self._pcap.open_live(interface, 65535, 1, 100)
        # filter, optimize, netmask
        self._pcap.setfilter(self.pcap_filter, 1, 0)

    # Iterate over individual dnsflow records (multiple per packet).
    # Skips stats pkts.
    def flow_iter(self):
        for pkt in self.pkt_iter():
            ts = pkt['header']['timestamp']
            if 'data' not in pkt:
                # stats pkt
                continue
            for record in pkt['data']:
                yield ts, record

    # Iterate over dnsflow pkts.
    def pkt_iter(self):
        while 1:
            rv = self._pcap.next()
            if rv == None:
                if self.pcap_file is not None:
                    # eof
                    break
                else:
                    # interface, hit to_ms
                    continue
            pktlen, buf, ts = rv
            pkt, err = process_pkt(self._pcap.datalink(), ts, buf,
                    stats_only=self.stats_only)
            if err is not None:
                print err
                continue
            yield pkt

#
# Returns a tuple(pkt_contents, error_string).
# error_string is None on success; on failure it contains a message
# describing the error.
# pkt_contents is a dict containing the unmarshaled data from the packet. It
# may be incomplete or empty on error.
# stats_only - set to True to parse stats pkts and headers only of data pkts.
def process_pkt(dl_type, ts, buf, stats_only=False):
    pkt = {}
    err = None

    if dl_type == dpkt.pcap.DLT_NULL:
        # Loopback
        try:
            lo = dpkt.loopback.Loopback(buf)
        except:
            err = 'LOOPBACK-PARSE-FAILED|%s' % (buf)
            return (pkt, err)
        if lo.family == socket.AF_UNSPEC:
            # dnsflow dumped straight to pcap
            dnsflow_pkt = lo.data
            ip_pkt = None
            src_ip = '0.0.0.0'
            src_port = 0
        elif lo.family == socket.AF_INET:
            # dl, ip, udp, dnsflow_pkt
            dnsflow_pkt = lo.data.data.data
            ip_pkt = lo.data
            src_ip = socket.inet_ntop(socket.AF_INET, ip_pkt.src)
            src_port = ip_pkt.data.sport
    elif dl_type == dpkt.pcap.DLT_EN10MB:
        # Ethernet
        try:
            eth = dpkt.ethernet.Ethernet(buf)
        except:
            err = 'ETHERNET-PARSE-FAILED|%s' % (buf)
            return (pkt, err)
        dnsflow_pkt = eth.data.data.data
        ip_pkt = eth.data
        src_ip = socket.inet_ntop(socket.AF_INET, ip_pkt.src)
        src_port = ip_pkt.data.sport

    cp = 0

    # vers, sets_count, flags, seq_num
    fmt = '!BBHI'
    try:
        vers, sets_count, flags, seq_num = struct.unpack(fmt,
                dnsflow_pkt[cp:cp + struct.calcsize(fmt)])
    except struct.error, e:
        err = 'PARSE_ERROR|%s|%s' % (fmt, e)
        return (pkt, err)
    cp += struct.calcsize(fmt)

    # Version 0, 1, or 2
    if (vers != 0 and vers != 1 and vers !=2) or sets_count == 0:
        err = 'BAD_PKT|%s' % (src_ip)
        return (pkt, err)
   
    hdr = {}
    hdr['src_ip'] = src_ip
    hdr['src_port'] = src_port
    hdr['timestamp'] = ts
    hdr['sets_count'] = sets_count
    hdr['flags'] = flags
    hdr['sequence_number'] = seq_num
    pkt['header'] = hdr
    
    if flags & DNSFLOW_FLAG_STATS:
        if vers == 2:
            fmt = '!5I'
        else:
            # vers 0 or 1
            fmt = '!4I'
        try:
            stats = struct.unpack(fmt,
                    dnsflow_pkt[cp:cp + struct.calcsize(fmt)])
        except struct.error, e:
            err = 'HEADER_PARSE_ERROR|%s|%s' % (fmt, e)
            return (pkt, err)
        sp = {}
        sp['pkts_captured'] = stats[0]
        sp['pkts_received'] = stats[1]
        sp['pkts_dropped'] = stats[2]
        sp['pkts_ifdropped'] = stats[3]
        if vers == 2:
            sp['sample_rate'] = stats[4]
        pkt['stats'] = sp

    elif not stats_only:
        # data pkt
        pkt['data'] = []
        for i in range(sets_count):
            # client_ip, names_count, ips_count, names_len
            fmt = '!IBBH'
            try:
                vals = struct.unpack(fmt,
                        dnsflow_pkt[cp:cp + struct.calcsize(fmt)])
                client_ip, names_count, ips_count, names_len = vals
            except struct.error, e:
                err = 'DATA_PARSE_ERROR|%s|%s' % (fmt, e)
                return (pkt, err)
            cp += struct.calcsize(fmt)
            client_ip = str(ipaddr.IPAddress(client_ip))

            fmt = '%ds' % (names_len)

            try:
                name_set = struct.unpack(fmt,
                        dnsflow_pkt[cp:cp + struct.calcsize(fmt)])[0]
            except struct.error, e:
                err = 'DATA_PARSE_ERROR|%s|%s' % (fmt, e)
                return (pkt, err)
            cp += struct.calcsize(fmt)
            if vers == 1 or vers == 2:
                # Each name is in the form of an uncompressed dns name.
                # names are root domain (Nul) terminated, and padded with Nuls
                # on the end to word align. 
                names = []
                np = 0
                try:
                    for x in range(names_count):
                        name = []
                        label_len = ord(name_set[np])
                        np += 1
                        while label_len != 0:
                            name.append(name_set[np: np + label_len])
                            np += label_len
                            label_len = ord(name_set[np])
                            np += 1
                        name = '.'.join(name)
                        names.append(name)
                except IndexError as e:
                    # Hit the end of the name_set buffer.
                    err = 'NAMES_PARSE_ERROR|%s|%d|%s' % (repr(name_set),
                            names_count, e)
                    return (pkt, err)
            else:
                # vers = 0
                # names are Nul terminated, and padded with Nuls on the end to
                # word align.
                names = name_set.split('\0')
                names = names[0:names_count]

            fmt = '!%dI' % (ips_count)
            try:
                ips = struct.unpack(fmt,
                        dnsflow_pkt[cp:cp + struct.calcsize(fmt)])
            except struct.error, e:
                err = 'DATA_PARSE_ERROR|%s|%s' % (fmt, e)
                return (pkt, err)
            cp += struct.calcsize(fmt)
            ips = [str(ipaddr.IPAddress(x)) for x in ips]

            data = {}
            data['client_ip'] = client_ip
            data['names'] = names
            data['ips'] = ips
            pkt['data'].append(data)

    return (pkt, err)

# Deprecated.
def read_pcapfiles(pcap_files, pcap_filter, callback):
    for pcap_file in pcap_files:
        print 'FILE|%s' % (pcap_file)
        # XXX dpkt pcap doesn't support filters and there's no way to pass
        # a gzip fd to pylibpcap. Bummer.
        p = pcap.pcapObject()
        p.open_offline(pcap_file)
        # filter, optimize, netmask
        # XXX This doesn't work with dump straight to pcap.
        # For now use -F "" in that case.
        p.setfilter(pcap_filter, 1, 0)

        while 1:
            rv = p.next()
            if rv == None:
                break
            pktlen, buf, ts = rv
            pkt, err = process_pkt(p.datalink(), ts, buf)
            if err is not None:
                print err
                continue
            callback(pkt)

# Deprecated.
def mode_livecapture(interface, pcap_filter, callback):
    print 'Capturing on', interface
    p = pcap.pcapObject()
    p.open_live(interface, 65535, 1, 100)
    # filter, optimize, netmask
    p.setfilter(pcap_filter, 1, 0)

    try:
        while 1:
            rv = p.next()
            if rv != None:
                pktlen, buf, ts = rv
                pkt, err = process_pkt(p.datalink(), ts, buf)
                if err is not None:
                    print err
                    continue
                callback(pkt)

    except KeyboardInterrupt:
        print '\nshutting down'
        print '%d packets received, %d packets dropped, %d packets dropped by interface' % p.stats()

def _print_parsed_pkt(pkt):
    hdr = pkt['header']
    ts = hdr['timestamp']
    tstr = time.strftime('%H:%M:%S', time.gmtime(ts))

    print 'HEADER|src=%s:%d|ts=%s|n_sets=%d|flags=%d|seq=%d' % (hdr['src_ip'],
            hdr['src_port'], tstr, hdr['sets_count'], hdr['flags'],
            hdr['sequence_number'])

    if 'stats' in pkt:
        stats = pkt['stats']
        print "STATS|%s" % ('|'.join(['%s:%d' % (x[0], x[1])
            for x in stats.items()]))
    else:
        for data in pkt['data']:
            print 'DATA|%s|%s|%s|%s' % (data['client_ip'], tstr,
                    ','.join(data['names']), ','.join(data['ips']))


class SrcTracker(object):
    def __init__(self):
        self.srcs = {}

    def update(self, pkt):
        hdr = pkt['header']
        src_id = (hdr['src_ip'], hdr['src_port'])
        src = self.srcs.get(src_id)
        if src is None:
            src = {
                    'n_records': 0,
                    'n_data_pkts': 0,
                    'n_stats_pkts': 0,
                    'first_timestamp': hdr['timestamp'],
                    'seq': {
                        'seq_last': None,
                        'seq_total': 0,
                        'seq_lost': 0,
                        'seq_ooo': 0,
                        }
                    }
            self.srcs[src_id] = src
        src['last_timestamp'] = hdr['timestamp']
        if 'stats' in pkt:
            src['n_stats_pkts'] += 1
            if 'stats_last' not in src:
                # First stats for src
                src['stats_last'] = pkt['stats']
                src['stats_delta_last'] = {}
                src['stats_delta_total'] = {}
                for k in pkt['stats'].iterkeys():
                    if k == 'sample_rate':
                        continue
                    src['stats_delta_total'][k] = 0
            for k in pkt['stats'].iterkeys():
                if k == 'sample_rate':
                    continue
                src['stats_delta_last'][k] = pkt['stats'][k] - src['stats_last'][k]
                src['stats_delta_total'][k] += src['stats_delta_last'][k]
            src['stats_last'] = pkt['stats']
        else:
            src['n_data_pkts'] += 1
            src['n_records'] += hdr['sets_count']

        # Track lost packets. Won't work if there are duplicates.
        src_seq = src['seq']
        src_seq['seq_total'] += 1
        seq_num = hdr['sequence_number']
        if src_seq['seq_last'] is None:
            src_seq['seq_last'] = seq_num
        elif seq_num == src_seq['seq_last'] + 1:
            src_seq['seq_last'] = seq_num
        elif seq_num > src_seq['seq_last'] + 1:
            src_seq['seq_lost'] += seq_num - src_seq['seq_last'] - 1
            src_seq['seq_last'] = seq_num
        elif seq_num < src_seq['seq_last']:
            src_seq['seq_lost'] -= 1
            src_seq['seq_ooo'] += 1
            # Don't update seq_last.

        return src_id

    def print_summary_src(self, src_id):
        src = self.srcs[src_id]
        ts_delta = src['last_timestamp'] - src['first_timestamp']
        print '%s:%s' % (src_id[0], src_id[1])
        print '  %s' % (' '.join(['%s=%d' % (k, src[k])
            for k in ['n_data_pkts', 'n_records', 'n_stats_pkts']]))
        if ts_delta > 0:
            print '  %s' % (' '.join(['%s/s=%.2f' % (k, src[k]/ts_delta)
                for k in ['n_data_pkts', 'n_records', 'n_stats_pkts']]))
        if 'stats_delta_total' in src:
            print '  %s' % (' '.join(['%s=%d' % (x[0], x[1])
                for x in src['stats_delta_total'].items()]))
            if ts_delta > 0:
                print '  %s' % (' '.join(['%s/s=%.2f' %
                    (x[0], x[1]/ts_delta)
                    for x in src['stats_delta_total'].items()]))
        print '  %s' % (' '.join(['%s=%d' % (x[0], x[1])
            for x in src['seq'].items()]))


    def print_summary(self):
        for src_id in self.srcs.iterkeys():
            self.print_summary_src(src_id)

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('-f', dest='extra_filter')
    p.add_argument('-F', dest='complete_filter')
    p.add_argument('-s', dest='stats_only', action='store_true', 
        help="show only status packets")
    p.add_argument('-S', dest='src_summary', action='store_true', 
        help="show source summaries")
    input_group = p.add_mutually_exclusive_group(required=True)
    input_group.add_argument('-r', dest='pcap_file')
    input_group.add_argument('-i', dest='interface')
    args = p.parse_args()

    return args

def main(argv):
    args = parse_args()

    pcap_filter = DEFAULT_PCAP_FILTER
    if args.extra_filter:
        pcap_filter = '(%s) and (%s)' % (DEFAULT_PCAP_FILTER, args.extra_filter)
    elif args.complete_filter:
        pcap_filter = args.complete_filter
    
    if args.stats_only or args.src_summary:
        # Only parse headers and stats pkts. I.e., skip payload of data pkts.
        parse_stats = True
    else:
        parse_stats = False

    if args.pcap_file:
        diter = pkt_iter(pcap_file=args.pcap_file, pcap_filter=pcap_filter,
                stats_only=parse_stats)
    else:
        diter = pkt_iter(interface=args.interface, pcap_filter=pcap_filter,
                stats_only=parse_stats)

    srcs = SrcTracker()
    try:
        for cnt, pkt in enumerate(diter):
            src_id = srcs.update(pkt)
            if args.stats_only:
                if 'stats' in pkt:
                    _print_parsed_pkt(pkt)
                    # XXX This is just printing the total so far, not since
                    # the last stats pkt.
                    srcs.print_summary_src(src_id)
            elif args.src_summary:
                if cnt != 0 and cnt % 100000 == 0:
                    srcs.print_summary()
                    print '-'*40
            else:
                _print_parsed_pkt(pkt)
    except KeyboardInterrupt:
        print '\nSummary:'
        srcs.print_summary()
    else:
        print '\nSummary:'
        srcs.print_summary()

if __name__ == '__main__':
    main(sys.argv)

