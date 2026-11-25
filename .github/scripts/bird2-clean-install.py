from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from shlex import quote
import sys
from threading import Thread

import pexpect


def run_guest(guest, command, timeout=60):
    guest.sendline(f"{command}; result=$?; printf '\\nBIRD_TEST_RESULT:%s\\n' \"$result\"")
    guest.expect(r"BIRD_TEST_RESULT:(\d+)\r*\n", timeout=timeout)
    if guest.match.group(1) != "0":
        raise RuntimeError(f"OpenWrt command failed ({guest.match.group(1)}): {command}")


def main():
    firmware, package = (Path(arg).resolve() for arg in sys.argv[1:])
    handler = partial(SimpleHTTPRequestHandler, directory=str(package.parent))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        args = [
            "-m", "512", "-smp", "2", "-display", "none", "-monitor", "none",
            "-serial", "stdio", "-drive", f"file={firmware},format=raw",
            "-nic", "user,model=e1000", "-no-reboot",
        ]
        with pexpect.spawn("qemu-system-x86_64", args, encoding="utf-8",
                           codec_errors="replace", timeout=150) as guest:
            guest.logfile_read = sys.stdout
            guest.expect("Please press Enter to activate this console")
            guest.sendline("")
            guest.expect(r"root@[^:\r\n]+:[^\r\n]*#")
            run_guest(guest, "grep -q \"DISTRIB_ID='OpenWrt'\" /etc/openwrt_release && ! apk info -e luci-app-bird")
            run_guest(guest, "for i in $(seq 1 30); do ubus call network.interface.lan status >/dev/null 2>&1 && exit_ready=yes && break; sleep 1; done; test \"$exit_ready\" = yes")
            run_guest(guest, "uci set network.lan.proto=dhcp && uci commit network && /etc/init.d/network restart")
            run_guest(guest, "for i in $(seq 1 30); do ip -4 route | grep -q '^default via 10.0.2.2' && break; sleep 1; done; "
                      "ip -4 route | grep -q '^default via 10.0.2.2'")
            url = f"http://10.0.2.2:{port}/{package.name}"
            run_guest(guest, f"wget -O /tmp/bird.apk {quote(url)} && apk add --allow-untrusted /tmp/bird.apk", 240)
            run_guest(guest, "apk info -e luci-app-bird && bird --version && "
                      "test -s /www/luci-static/resources/view/bird/bird.js && "
                      "grep -Fq 'admin/services/bird' /usr/share/luci/menu.d/luci-app-bird.json")
            run_guest(guest, "test \"$(ubus call luci.ospf status | jsonfilter -e '@.mode')\" = external")
            settings = {
                "enabled": True, "router_id": "192.0.2.10", "policy": "none", "prefixes": [],
                "takeover": True, "links": [{
                    "device": "br-lan", "area": "0", "mode": "passive", "family": "ipv4",
                    "cost": 10, "priority": 1, "hello": 10, "dead": 40,
                }],
            }
            request = json.dumps({
                "revision": "REVISION", "payload": json.dumps(settings, separators=(",", ":"))
            }, separators=(",", ":"))
            run_guest(guest, "rev=$(ubus call luci.ospf status | jsonfilter -e '@.revision'); "
                      f"req=$(printf %s {quote(request)} | sed \"s/REVISION/$rev/\"); "
                      "test \"$(ubus call luci.ospf apply \"$req\" | jsonfilter -e '@.ok')\" = true")
            run_guest(guest, "test \"$(ubus call luci.ospf status | jsonfilter -e '@.mode')\" = managed && "
                      "bird -p -c /etc/bird.conf && birdc show protocols | grep -Eq '^lan_ospf[[:space:]].*up'")
            cmd = "ubus call session access " + quote(json.dumps({
                "ubus_rpc_session": "0" * 32, "scope": "ubus", "object": "luci.ospf", "function": "native_read",
            }))
            run_guest(guest, f"test \"$({cmd} | jsonfilter -e '@.access')\" = false")
            run_guest(guest, "grep -m1 -q '^# luci-app-bird: ' /etc/bird.conf")
            print("PASS: official OpenWrt clean install, LuCI endpoint, OSPF apply, and ACL")


if __name__ == "__main__":
    main()
