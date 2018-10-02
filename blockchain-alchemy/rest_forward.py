import json
import logging
import os
import requests
import re

from webob import Response
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import dpset
from ryu.controller.handler import set_ev_cls
from ryu.exception import OFPUnknownVersion
from ryu.lib import dpid as dpid_lib
from ryu.lib import ofctl_v1_3
from ryu.ofproto import ofproto_v1_3, ofproto_v1_3_parser
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.lib import hub

rules_default = 'channels.json'
rules = 'channels.json'
LOG = logging.getLogger(__name__)


class ForwardRest(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'dpset': dpset.DPSet,
                 'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(ForwardRest, self).__init__(*args, **kwargs)
        self.switches = {}
        dpset = kwargs['dpset']
        wsgi = kwargs['wsgi']
        self.datapaths = {}
        self.data = {}
        self.waiters = {}
        self.data['dpset'] = dpset
        self.data['waiters'] = self.waiters
        ForwardController.set_logger(self.logger)
        wsgi.registory['ForwardController'] = self.data
        wsgi.register(ForwardController, self.data)
        self.monitor_thread = hub.spawn(self._monitor)

    @set_ev_cls(dpset.EventDP, dpset.DPSET_EV_DISPATCHER)
    def handler_datapath(self, ev):
        if ev.enter:
            ForwardController.regist_ofs(ev.dp)
        else:
            ForwardController.unregist_ofs(ev.dp)

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if datapath.id not in self.datapaths:
                self.logger.debug('register datapath: %016x', datapath.id)
                self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                self.logger.debug('unregister datapath: %016x', datapath.id)
                del self.datapaths[datapath.id]

    def _monitor(self):
        while True:
            for dp in self.datapaths.values():
                self._request_stats(dp)
            hub.sleep(10)

    def _request_stats(self, datapath):
        self.logger.debug('send stats request: %016x', datapath.id)
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        req = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req)

        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)


    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        body = ev.msg.body

        self.logger.info('datapath         '
                         'in-port  eth-dst           '
                         'out-port packets  bytes')
        self.logger.info('---------------- '
                         '-------- ----------------- '
                         '-------- -------- --------')
        for stat in sorted([flow for flow in body if flow.priority == 1000]):
            self.logger.info('%016x %8x %17s %8x %8d %8d',
                             ev.msg.datapath.id,
                             stat.match['in_port'], 0,
                             0,
                             stat.packet_count, stat.byte_count)
            ForwardController._STATS[ev.msg.datapath.id] = stat.packet_count

class ForwardOfsList(dict):
    def __init__(self):
        super(ForwardOfsList, self).__init__()

    def get_ofs(self, dp_id):
        if len(self) == 0:
            raise ValueError('forwarding sw is not connected.')

        dps = {}

        try:
            dpid = dpid_lib.str_to_dpid(dp_id)
        except:
            raise ValueError('Invalid switchID.')

        if dpid in self:
            dps = {dpid: self[dpid]}
        else:
            msg = 'forwarding sw is not connected. : switchID=%s' % dp_id
            raise ValueError(msg)

        return dps


class ForwardController(ControllerBase):
    _OFS_LIST = ForwardOfsList()
    _LOGGER = None
    _STATS = {}
    def __init__(self, req, link, data, **config):
        super(ForwardController, self).__init__(req, link, data, **config)
        self.name = self.__class__.__name__
        self.logger = logging.getLogger(self.name)
        self.dpset = data['dpset']
        self.waiters = data['waiters']

        try:
            jsondict = json.load(open(rules))
            self.logger.info("JSON configuration loaded from %s:" % rules)
        except IOError:
            try:
                jsondict = json.load(open(rules_default))
                self.logger.info("JSON configuration loaded from %s:" % rules_default)
            except IOError:
                self.logger.info("Can't open %s and %s" % (rules, rules_default))
                self.ovs = {}
                self.channels = {}
        try:
            self.ovs = jsondict["ovs"]
            self.channels = jsondict["channels"]
            self.logger.info(json.dumps(jsondict))
        except ValueError:
            self.logger.info("JSON syntaxis error in %s" % rules)
            self.ovs = {}
            self.channels = {}

    @classmethod
    def set_logger(cls, logger):
        cls._LOGGER = logger
        cls._LOGGER.propagate = False
        hdlr = logging.StreamHandler()
        fmt_str = '[FW][%(levelname)s] %(message)s'
        hdlr.setFormatter(logging.Formatter(fmt_str))
        cls._LOGGER.addHandler(hdlr)

    @staticmethod
    def regist_ofs(dp):
        dpid_str = dpid_lib.dpid_to_str(dp.id)
        try:
            f_ofs = Forward(dp)
        except OFPUnknownVersion as message:
            ForwardController._LOGGER.info('dpid=%s: %s',
                                           dpid_str, message)
            return

        ForwardController._OFS_LIST.setdefault(dp.id, f_ofs)
        ForwardController._LOGGER.info('dpid=%s: Forwarding module is joined.',
                                       dpid_str)

    @staticmethod
    def unregist_ofs(dp):
        if dp.id in ForwardController._OFS_LIST:
            del ForwardController._OFS_LIST[dp.id]
            ForwardController._LOGGER.info('dpid=%s: Forwarding module is disconnected.',
                                           dpid_lib.dpid_to_str(dp.id))


    @route('status', '/status', methods=['GET'])
    def return_status(self, req, **kwargs):
        return Response(content_type='application/text', body='1')

    @route('channels', '/channels', methods=['GET'])
    def return_channels(self, req, **kwargs):
        return Response(content_type='application/text', body=str(len(self.channels.values())))

    @route('statistics', '/statistics', methods=['GET'])
    def return_statistics(self, req, **kwargs):
        result = 0
        for c in self._STATS.values():
            result+=c
        return Response(content_type='application/text', body=str(result))

    @route('activate', '/activate', methods=['GET'])
    def activ_rules(self, req, **kwargs):
        mode = 'add'
        for c in self.channels.values():
            if c["active"] == "1":
                f = self.set_data(c, mode)

        body = json.dumps({'result': 'Rules are activated.'})
        return Response(content_type='application/json', body=body)

    @route('channel', '/channel', methods=['POST'])
    def set_channel(self, req, **kwargs):
        jsonconf = json.loads(req.body)
        result = []
        for l in jsonconf.keys():
            ch = l
            for k, v in self.channels.items():
                if k == ch:
                    c = self.channels[ch]
                    mode = 'delete'
                    self.set_data(c, mode)
            self.channels[ch] = jsonconf[ch]

            mode = 'add'
            c = self.channels[ch]
            self.set_data(c, mode)
            result.append({'id': ch, 'result': 'Configurations are added.'})
        body = json.dumps(result)
        f = open(rules, "w+")
        f.write(json.dumps({"ovs": self.ovs, "channels": self.channels}))
        f.close()
        return Response(content_type='application/json', body=body)

    @route('ovs', '/ovs', methods=['POST'])
    def set_ovs(self, req, **kwargs):
        jsonconf = json.loads(req.body)
        self.ovs.update(jsonconf)

        f = open(rules, "w+")
        f.write(json.dumps({"ovs": self.ovs, "channels": self.channels}))
        f.close()
        body = json.dumps({'result': 'OVS updated.'})
        return Response(content_type='application/json', body=body)

    @route('channel', '/channel/{channel}/{sla}', methods=['GET'], requirements={'channel': r'[0-9]', 'sla': r'[0-9]'})
    def get_channel(self, req, **kwargs):
        sla_id = kwargs['sla']
        ch = kwargs['channel']
        mode = 'delete'
        try:
            c = self.channels[ch]
        except KeyError:
            message = "channel doesn't exists"
            return Response(status=400, body=str(message))

        self.set_data(c, mode)
        if sla_id == "0":
            self.channels[ch]["active"] = "0"
        else:
            self.channels[ch]["active"] = "1"
            self.channels[ch]["sla"] = sla_id
            mode = 'add'
            self.set_data(c, mode)
        f = open(rules, "w+")
        f.write(json.dumps({"ovs": self.ovs, "channels": self.channels}))
        f.close()

        body = json.dumps({'id': ch, 'result': 'Switched to channel: %d' % int(sla_id)})
        return Response(content_type='application/json', body=body)

    @route('qkey', '/qkey/{ovs}/{sla}', methods=['POST'],
           requirements={'channel': r'[0-9]'})
    def post_handler(self, req, **kwargs):

        # In JSON file channel number is the key (string)
        ovs_id = str(kwargs['ovs'])
        sla_id = str(kwargs['sla'])
        ch = self.ovs[ovs_id]["outport"]
        mzvs_id = None
        for k, v in ch.items():
            for k2, v2 in v.items():
                if (k2 == "sla") & (v2 == sla_id):
                    mzvs_id = str(ch[k]["mzvs"])
                    print mzvs_id
                    break
            if mzvs_id is not None:
                break

        qkey = req.body
        addr = req.remote_addr

        body = json.dumps({'mzvs': mzvs_id,
                           'qkey': qkey,
                           'addr': addr})

        body = "ADDR: %s QKEY: %s\n" % (addr, qkey)

        self.logger.info("body: " + body)
        if mzvs_id is not None:
            for url in self.ovs[ovs_id]["mzvs"][mzvs_id]:
                body += "URL %s RESULT:\n" % url
                try:
                    r = requests.post(url, data=qkey.encode("utf-8"))
                    body += r.text
                except:  # requests.ConnectionError:
                    body += "URL %s: can't connect\n" % url
        else:
            body = "Cannot find sla_id: %s\n" % sla_id
        return Response(content_type='application/json', body=body.encode('utf-8'))

    def set_data(self, c, mode):
        ip_src = c["src"]["subnet"]
        ip_dst = c["dst"]["subnet"]
        vlan_src = c["src"]["vlan"]
        vlan_dst = c["dst"]["vlan"]
        ovs_src = str(c["src"]["ovs"])
        dp1 = self.ovs[ovs_src]["dpid"]
        machine = c["src"]["type"]
        for k, v in self.ovs[ovs_src]["intport"].items():
            if v == {'type': machine}:
                try:
                    inport1 = int(k)
                except ValueError:
                    inport1 = k

        sla_id = str(c["sla"])
        for k, v in self.ovs[ovs_src]["outport"].items():
            for k2, v2 in v.items():
                if v2 == sla_id:
                    try:
                        outport1 = int(k)
                    except ValueError:
                        outport1 = k

        f = self._set_rule(dp1, ip_src, ip_dst, vlan_src, vlan_dst, inport1, outport1, mode)

        ovs_dst = str(c["dst"]["ovs"])
        dp2 = self.ovs[ovs_dst]["dpid"]
        machine = c["dst"]["type"]
        for k, v in self.ovs[ovs_dst]["intport"].items():
            if v == {'type': machine}:
                try:
                    inport2 = int(k)
                except ValueError:
                    inport2 = k
        for k, v in self.ovs[ovs_dst]["outport"].items():
            for k2, v2 in v.items():
                if v2 == sla_id:
                    try:
                        outport2 = int(k)
                    except ValueError:
                        outport2 = k
        f = self._set_rule(dp2, ip_dst, ip_src, vlan_dst, vlan_src, inport2, outport2, mode)
        return f

    def _set_rule(self, dp, ip_src, ip_dst, vlan_src, vlan_dst, inport, outport, mode):

        try:
            dps = self._OFS_LIST.get_ofs(dp)  # returns dict {switchid : Forward(switchid)}
        except ValueError as message:
            return Response(status=400, body=str(message))

        msgs = []
        for f_ofs in dps.values():
            try:
                flow = f_ofs.set_rule(ip_src, ip_dst, vlan_src, vlan_dst, inport, outport, mode)

            except ValueError as message:
                return Response(status=400, body=str(message))

        return flow


class Forward(object):
    _OFCTL = {ofproto_v1_3.OFP_VERSION: ofctl_v1_3}

    def __init__(self, dp):
        super(Forward, self).__init__()
        self.dp = dp
        version = dp.ofproto.OFP_VERSION

        if version not in self._OFCTL:
            raise OFPUnknownVersion(version=version)

        self.ofctl = self._OFCTL[version]

    def set_rule(self, ip_src, ip_dst, vlan_src, vlan_dst, inport, outport, mode):
        flows = []
        if mode == 'add':
            cmd = self.dp.ofproto.OFPFC_ADD
        elif mode == 'delete':
            cmd = self.dp.ofproto.OFPFC_DELETE_STRICT
        if vlan_src > 0:
            match = {"in_port": inport, "dl_vlan": int(vlan_src), "ipv4_src": ip_src,
                     "ipv4_dst": ip_dst, "eth_type": 2048}
        else:
            match = {"in_port": inport, "ipv4_src": ip_src,
                     "ipv4_dst": ip_dst, "eth_type": 2048}
        if vlan_dst > 0:
            actions = [{"type": "POP_VLAN", "ethertype": 33024}, {"type": "PUSH_VLAN", "ethertype": 33024},
                       {"type": "SET_FIELD", "field": "vlan_vid", "value": 4096 + int(vlan_dst)},
                       {"type": "OUTPUT", "port": outport}]
        else:
            if vlan_src != vlan_dst:
                actions = [{"type": "SET_FIELD", "field": "vlan_vid", "value": 4096 + int(vlan_dst)},
                           {"type": "OUTPUT", "port": outport}]
            else:
                actions = [{"type": "OUTPUT", "port": outport}]

        flow = self._to_of_flow(priority=1000,
                                match=match, actions=actions)
        flows.append(flow)

        try:
            ofctl_v1_3.mod_flow_entry(self.dp, flow, cmd)
        except:
            raise ValueError('Invalid rule parameter.')

        if vlan_src > 0:
            match = {"in_port": inport, "dl_vlan": int(vlan_src), "arp_spa": ip_src,
                     "arp_tpa": ip_dst, "eth_type": 2054}
        else:
            match = {"in_port": inport, "arp_spa": ip_src,
                     "arp_tpa": ip_dst, "eth_type": 2054}
        if vlan_dst > 0:
            actions = [{"type": "POP_VLAN", "ethertype": 33024}, {"type": "PUSH_VLAN", "ethertype": 33024},
                       {"type": "SET_FIELD", "field": "vlan_vid", "value": 4096 + int(vlan_dst)},
                       {"type": "OUTPUT", "port": outport}]
        else:
            if vlan_src != vlan_dst:
                actions = [{"type": "SET_FIELD", "field": "vlan_vid", "value": 4096 + int(vlan_dst)},
                           {"type": "OUTPUT", "port": outport}]
            else:
                actions = [{"type": "OUTPUT", "port": outport}]

        flow = self._to_of_flow(priority=1000,
                                match=match, actions=actions)
        flows.append(flow)
        try:
            self.ofctl.mod_flow_entry(self.dp, flow, cmd)
        except:
            raise ValueError('Invalid rule parameter.')

        if vlan_src > 0:
            match = {"in_port": outport, "dl_vlan": int(vlan_src), "ipv4_dst": ip_src,
                     "ipv4_src": ip_dst, "eth_type": 2048}
        else:
            match = {"in_port": outport, "ipv4_dst": ip_src,
                     "ipv4_src": ip_dst, "eth_type": 2048}
        actions = [{"type": "OUTPUT", "port": inport}]

        flow = self._to_of_flow(priority=1000,
                                match=match, actions=actions)
        flows.append(flow)
        try:
            self.ofctl.mod_flow_entry(self.dp, flow, cmd)
        except:
            raise ValueError('Invalid rule parameter.')

        if vlan_src > 0:
            match = {"in_port": outport, "dl_vlan": int(vlan_src), "arp_tpa": ip_src,
                     "arp_spa": ip_dst, "eth_type": 2054}
        else:
            match = {"in_port": outport, "arp_tpa": ip_src,
                     "arp_spa": ip_dst, "eth_type": 2054}

        actions = [{"type": "OUTPUT", "port": inport}]

        flow = self._to_of_flow(priority=1000,
                                match=match, actions=actions)
        flows.append(flow)
        try:
            self.ofctl.mod_flow_entry(self.dp, flow, cmd)
        except:
            raise ValueError('Invalid rule parameter.')

        msg = {'result': 'success'}
        return msg

    def _to_of_flow(self, priority, match, actions):
        flow = {'cookie': 0,
                'priority': priority,
                'flags': 0,
                'idle_timeout': 0,
                'hard_timeout': 0,
                'match': match,
                'actions': actions}
        return flow