#!/usr/bin/python3
# Copyright (c) 2008 Alon Swartz <alon@turnkeylinux.org> - all rights reserved
"""TurnKey Configuration Console

Options:
    -h, --help           Display this help and exit
        --usage          Display usage screen without Advanced Menu
        --nointeractive  Do not display interactive dialog
        --plugin=<name>  Run plugin directly

"""

import os
import sys
import dialog
from dialog import DialogError
from string import Template

import getopt
from io import StringIO
import traceback
import subprocess
import shlex
from typing import NoReturn, Optional, Callable

import netinfo

import conf
import ifutil
import ipaddr
import plugin

PLUGIN_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                           'plugins.d')


class Error(Exception):
    pass


def fatal(e: str) -> NoReturn:
    print("error:", e, file=sys.stderr)
    sys.exit(1)


def usage(e: str = None) -> NoReturn:
    if e:
        print("Error:", e, file=sys.stderr)

    print(f"Syntax: {sys.argv[0]}", file=sys.stderr)
    print(__doc__.strip(), file=sys.stderr)
    sys.exit(1)


def format_fields(fields: list[tuple[str, int, int, int]]
                  ) -> list[tuple[str, int, int, int, int, int, int, int]]:
    '''Takes fields in format (label, field, label_length, field_length) and
    outputs fields in format (label, ly, lx, item, iy, ix, field_length,
    input_length)
    '''
    out = []
    for i, (label, field, l_length, f_length) in enumerate(fields):
        out.append((label, i+1, 1, field, i+1, l_length+1, l_length, f_length))
    return out


class Console:
    def __init__(self, title: str = None, width: int = 60, height: int = 20):
        self.width = width
        self.height = height

        self.console = dialog.Dialog(dialog="dialog")
        self.console.add_persistent_args(["--no-collapse"])
        self.console.add_persistent_args(["--ok-label", "Select"])
        self.console.add_persistent_args(["--cancel-label", "Back"])
        self.console.add_persistent_args(["--colors"])
        if conf.Conf().copy_paste:
            self.console.add_persistent_args(["--no-mouse"])
        if title:
            self.console.add_persistent_args(["--backtitle", title])

    def _handle_exitcode(self, retcode: str) -> bool:
        if retcode == 'esc':
            text = "Do you really want to quit?"
            if self.console.yesno(text) == 0:
                sys.exit(0)
            return False
        return True

    def _wrapper(self, dialog: str, text: str, *args, **kws
                 ) -> tuple[str, str]:
        try:
            method = getattr(self.console, dialog)
        except AttributeError:
            raise Error("dialog not supported: " + dialog)

        while 1:
            try:
                ret = method("\n" + text, *args, **kws)
            except DialogError as e:
                if "Can't make new window" in e.message:
                    self.console.msgbox(
                        "Terminal too small for UI, resize terminal and press"
                        " OK",
                        ok_label='OK'
                    )
                    continue
                else:
                    raise

            if type(ret) is str:
                retcode = ret
            else:
                retcode = ret[0]

            if self._handle_exitcode(retcode):
                break

        return ret

    def infobox(self, text: str) -> tuple[str, str]:
        return self._wrapper("infobox", text)

    def yesno(self, text: str, autosize: bool = False
              ) -> tuple[str, str]:
        if autosize:
            text += '\n '
            height, width = 0, 0
        else:
            height, width = 10, 30
        return self._wrapper("yesno", text, height, width)

    def msgbox(self, title: str, text: str, button_label: str = "ok",
               autosize: bool = False
               ) -> tuple[str, str]:
        if autosize:
            text += '\n '
            height, width = 0, 0
        else:
            height, width = self.height, self.width

        return self._wrapper("msgbox", text, height, width,
                             title=title, ok_label=button_label)

    def inputbox(self, title: str, text: str, init: str = '',
                 ok_label: str = "OK", cancel_label: str = "Cancel"
                 ) -> tuple[str, str]:
        no_cancel = True if cancel_label == "" else False
        return self._wrapper("inputbox", text, self.height, self.width,
                             title=title, init=init, ok_label=ok_label,
                             cancel_label=cancel_label, no_cancel=no_cancel)

    def menu(self, title: str, text: str, choices: list[tuple[str, str]],
             no_cancel: bool = False
             ) -> tuple[str, str]:
        return self._wrapper("menu", text, self.height, self.width,
                             menu_height=len(choices)+1,
                             title=title, choices=choices, no_cancel=no_cancel)

    def form(self, title: str, text: str, fields: list[str],
             ok_label: str = "Apply", cancel_label: str = "Cancel",
             autosize: bool = False
             ) -> tuple[str, str]:
        if autosize:
            text += '\n '
            height, width = 0, 0
        else:
            height, width = self.height, self.width
        return self._wrapper("form", text, fields,
                             height=height, width=width,
                             form_height=len(fields)+1,
                             title=title,
                             ok_label=ok_label, cancel_label=cancel_label)


class Installer:
    def __init__(self, path: str):
        self.path = path
        self.available = self._is_available()

    def _is_available(self) -> bool:
        if not os.path.exists(self.path):
            return False

        with open('/proc/cmdline') as fob:
            return 'boot=live' in fob.readline().split()

        return False

    def execute(self) -> None:
        if not self.available:
            raise Error("installer is not available to be executed")

        subprocess.run([self.path])


class TurnkeyConsole:
    OK = 'ok'
    CANCEL = 1

    def __init__(self, pluginManager: plugin.PluginManager,
                 eventManager: str, advanced_enabled: bool = True):
        title = "TurnKey GNU/Linux Configuration Console"
        self.width = 60
        self.height = 20

        self.console = Console(title, self.width, self.height)
        self.appname = f"TurnKey GNU/Linux {netinfo.get_hostname().upper()}"

        self.installer = Installer(path='/usr/bin/di-live')

        self.advanced_enabled = advanced_enabled

        # self.eventManager = plugin.EventManager()
        # self.pluginManager = plugin.PluginManager(
        #     PLUGIN_PATH,
        #     {'eventManager': self.eventManager, 'console': self.console})

        self.eventManager = eventManager
        self.pluginManager = pluginManager
        self.pluginManager.updateGlobals({'console': self.console})

    @staticmethod
    def _get_filtered_ifnames() -> list[str]:
        ifnames = []
        for ifname in netinfo.get_ifnames():
            if ifname.startswith(('lo', 'tap', 'br', 'natbr', 'tun',
                                  'vmnet', 'veth', 'wmaster')):
                continue
            ifnames.append(ifname)

        # handle bridged LXC where br0 is the default outward-facing interface
        defifname = conf.Conf().default_nic
        if defifname and defifname.startswith('br'):
            ifnames.append(defifname)
            bridgedif = subprocess.check_output(
                    ['brctl', 'show', defifname],
                    text=True
                    ).split('\n')[1].split('\t')[-1]
            ifnames.remove(bridgedif)

        ifnames.sort()
        return ifnames

    @classmethod
    def _get_default_nic(cls) -> Optional[str]:
        def _validip(ifname):
            ip = ifutil.get_ipconf(ifname)[0]
            if ip and not ip.startswith('169'):
                return True
            return False

        defifname = conf.Conf().default_nic
        if defifname and _validip(defifname):
            return defifname

        for ifname in cls._get_filtered_ifnames():
            if _validip(ifname):
                return ifname

        return None

    @classmethod
    def _get_public_ipaddr(cls) -> Optional[str]:
        publicip_cmd = conf.Conf().publicip_cmd
        if publicip_cmd:
            command = subprocess.run(shlex.split(publicip_cmd),
                                     capture_output=True, text=True)
            if command.returncode == 0:
                return command.stdout.strip()

        return None

    def _get_advmenu(self) -> tuple[list[tuple[str, str]], dict[str, str]]:
        items = []
        if conf.Conf().networking:
            items.append(("Networking", "Configure appliance networking"))

        if self.installer.available:
            items.append(("Install", "Install to hard disk"))

        plugin_map = {}

        for path in self.pluginManager.path_map:
            plug = self.pluginManager.path_map[path]
            if os.path.dirname(path) == PLUGIN_PATH:
                if isinstance(plug, plugin.Plugin) and hasattr(plug.module,
                                                               'run'):
                    items.append((plug.module_name.capitalize(),
                                  str(plug.module.__doc__)))
                elif isinstance(plug, plugin.PluginDir):
                    items.append((plug.module_name.capitalize(),
                                  plug.description))
                plugin_map[plug.module_name.capitalize()] = plug

        items.append(("Reboot", "Reboot the appliance"))
        items.append(("Shutdown", "Shutdown the appliance"))
        items.append(("Quit", "Quit the configuration console"))

        return items, plugin_map

    def _get_netmenu(self) -> list[tuple[str, str]]:
        menu = []
        for ifname in self._get_filtered_ifnames():
            addr = ifutil.get_ipconf(ifname)[0]
            ifmethod = ifutil.get_ifmethod(ifname)

            if addr:
                desc = str(addr)
                if ifmethod:
                    desc += f" ({ifmethod})"

                if ifname == self._get_default_nic():
                    desc += " [*]"
            else:
                desc = "not configured"

            menu.append((ifname, desc))

        return menu

    def _get_ifconfmenu(self, ifname: str) -> list[tuple[str, str]]:
        menu = []
        menu.append(("DHCP", "Configure networking automatically"))
        menu.append(("StaticIP", "Configure networking manually"))

        if not ifname == self._get_default_nic() and \
           len(self._get_filtered_ifnames()) > 1 and \
           ifutil.get_ipconf(ifname)[0] is not None:
            menu.append(("Default", "Show this adapter's IP address in Usage"))

        return menu

    def _get_ifconftext(self, ifname: str) -> str:
        addr, netmask, gateway, nameservers = ifutil.get_ipconf(ifname)
        if addr is None:
            return "Network adapter is not configured\n"

        text = f"IP Address:      {addr}\n"
        text += f"Netmask:         {netmask}\n"
        text += f"Default Gateway: {gateway}\n"
        text += f"Name Server(s):  {' '.join(nameservers)}\n\n"

        ifmethod = ifutil.get_ifmethod(ifname)
        if ifmethod:
            text += f"Networking configuration method: {ifmethod}\n"

        if len(self._get_filtered_ifnames()) > 1:
            text += "Is this adapter's IP address displayed in Usage: "
            if ifname == self._get_default_nic():
                text += "yes\n"
            else:
                text += "no\n"

        return text

    def usage(self) -> str:
        if self.advanced_enabled:
            default_button_label = "Advanced Menu"
            default_return_value = "advanced"
        else:
            default_button_label = "Quit"
            default_return_value = "quit"

        # if no interfaces at all - display error and go to advanced
        if len(self._get_filtered_ifnames()) == 0:
            error = "No network adapters detected"
            if not self.advanced_enabled:
                fatal(error)

            self.console.msgbox("Error", error)
            return "advanced"

        # if interfaces but no default - display error and go to networking
        ifname = self._get_default_nic()
        if not ifname:
            error = "Networking is not yet configured"
            if not self.advanced_enabled:
                fatal(error)

            self.console.msgbox("Error", error)
            return "networking"

        # tklbam integration
        tklbamstatus_cmd = subprocess.run(['which', 'tklbam-status'],
                                          capture_output=True,
                                          text=True).stdout.strip()
        if tklbamstatus_cmd:
            tklbam_status = subprocess.run([tklbamstatus_cmd, "--short"],
                                           capture_output=True,
                                           text=True).stdout
        else:
            tklbam_status = ("TKLBAM not found - please check that it's"
                             " installed.")

        # display usage
        ip_addr = self._get_public_ipaddr()
        if not ip_addr:
            ip_addr = ifutil.get_ipconf(ifname)[0]

        hostname = netinfo.get_hostname().upper()

        try:
            with open(conf.path('services.txt'), 'r') as fob:
                t = fob.read().rstrip()
        except conf.Error:
            t = ""
        text = Template(t).substitute(ipaddr=ip_addr)

        text += f"\n\n{tklbam_status}\n\n"
        text += "\n" * (self.height - len(text.splitlines()) - 7)
        text += "         TurnKey Backups and Cloud Deployment\n"
        text += "             https://hub.turnkeylinux.org"

        retcode = self.console.msgbox(f"{hostname} appliance services",
                                      text,
                                      button_label=default_button_label)

        if retcode is not self.OK:
            self.running = False

        return default_return_value

    def advanced(self) -> str:
        # dont display cancel button when no interfaces at all
        no_cancel = False
        if len(self._get_filtered_ifnames()) == 0:
            no_cancel = True

        items, plugin_map = self._get_advmenu()

        retcode, choice = self.console.menu("Advanced Menu",
                                            self.appname + " Advanced Menu\n",
                                            items,
                                            no_cancel=no_cancel)

        if retcode is not self.OK:
            return "usage"

        if choice in plugin_map:
            return plugin_map[choice].path

        return "_adv_" + choice.lower()

    def networking(self) -> str:
        ifnames = self._get_filtered_ifnames()

        # if no interfaces at all - display error and go to advanced
        if len(ifnames) == 0:
            self.console.msgbox("Error", "No network adapters detected")
            return "advanced"

        # if only 1 interface, dont display menu - just configure it
        if len(ifnames) == 1:
            self.ifname = ifnames[0]
            return "ifconf"

        # display networking
        text = "Choose network adapter to configure\n"
        if self._get_default_nic():
            text += "[*] This adapter's IP address is displayed in Usage"

        retcode, self.ifname = self.console.menu("Networking configuration",
                                                 text, self._get_netmenu())

        if retcode is not self.OK:
            return "advanced"

        return "ifconf"

    def ifconf(self) -> str:
        retcode, choice = self.console.menu("{self.ifname} configuration",
                                            self._get_ifconftext(self.ifname),
                                            self._get_ifconfmenu(self.ifname))

        if retcode is not self.OK:
            # if multiple interfaces go back to networking
            if len(self._get_filtered_ifnames()) > 1:
                return "networking"

            return "advanced"

        return "_ifconf_" + choice.lower()

    def _ifconf_staticip(self) -> str:
        def _validate(addr: ipaddr.IP, netmask: ipaddr.IP, gateway: ipaddr.IP,
                      nameservers: list[ipaddr.IP]
                      ) -> list[str]:
            """Validate Static IP form parameters. Returns an empty array on
               success, an array of strings describing errors otherwise"""

            errors = []
            if not addr:
                errors.append("No IP address provided")
            elif not ipaddr.is_legal_ip(addr):
                errors.append(f"Invalid IP address: {addr}")

            if not netmask:
                errors.append("No netmask provided")
            elif not ipaddr.is_legal_ip(netmask):
                errors.append(f"Invalid netmask: {netmask}")

            for nameserver in nameservers:
                if nameserver and not ipaddr.is_legal_ip(nameserver):
                    errors.append(f"Invalid nameserver: {nameserver}")

            if len(nameservers) != len(set(nameservers)):
                errors.append("Duplicate nameservers specified")

            if errors:
                return errors

            if gateway:
                if not ipaddr.is_legal_ip(gateway):
                    return [f"Invalid gateway: {gateway}"]
                else:
                    iprange = ipaddr.IPRange(addr, netmask)
                    if gateway not in iprange:
                        return [f"Gateway ({gateway}) not in IP range"
                                f" ({iprange})"]
            return []

        warnings = []
        try:
            addr, netmask, gateway, nameservers \
                    = ifutil.get_ipconf(self.ifname, True)
        except subprocess.CalledProcessError:
            warnings.append(
                '`route -n` failed - unable to get gateway')
            addr, netmask, gateway, nameservers = None, None, '', []
        except netinfo.NetInfoError:
            warnings.append('failed to find default gateway!')
            addr, netmask, gateway, nameservers = None, None, '', []

        if addr is None:
            warnings.append('failed to assertain current address!')
            addr = ''
        if netmask is None:
            warnings.append('failed to assertain current netmask!')
            netmask = ''

        if warnings:
            warnings.append('\nWill leave relevant fields blank')

        if warnings:
            self.console.msgbox("Warning", '\n'.join(warnings))

        input = [addr, netmask, gateway]
        input.extend(nameservers)

        # include minimum 2 nameserver fields and 1 blank one
        if len(input) < 4:
            input.append('')

        if input[-1]:
            input.append('')

        field_width = 30
        field_limit = 15

        while 1:
            fields = [
                ("IP Address", input[0], field_width, field_limit),
                ("Netmask", input[1], field_width, field_limit),
                ("Default Gateway", input[2], field_width, field_limit),
            ]

            for i in range(len(input[3:])):
                fields.append(("Name Server", input[3+i],
                               field_width, field_limit))

            fields = format_fields(fields)
            text = f"Static IP configuration ({self.ifname})"
            retcode, input = self.console.form("Network settings",
                                               text, fields)

            if retcode is not self.OK:
                break

            # remove any whitespaces the user might of included
            for i in range(len(input)):
                input[i] = input[i].strip()

            # unconfigure the nic if all entries are empty
            if not input[0] and not input[1] and not input[2] and not input[3]:
                ifutil.unconfigure_if(self.ifname)
                break

            addr, netmask, gateway = input[:3]
            nameservers = input[3:]
            for i in range(nameservers.count('')):
                nameservers.remove('')

            err = _validate(addr, netmask, gateway, nameservers)
            if err:
                err = "\n".join(err)
            else:
                in_ssh = 'SSH_CONNECTION' in os.environ
                if not in_ssh or (
                    in_ssh and self.console.yesno(
                        "Warning: Changing ip while an ssh session is active"
                        " will drop said ssh session!", autosize=True
                        ) == self.OK):
                    err = ifutil.set_static(self.ifname, addr, netmask,
                                            gateway, nameservers)
                    if not err:
                        break
                else:
                    break

            self.console.msgbox("Error", err)

        return "ifconf"

    def _ifconf_dhcp(self) -> str:
        in_ssh = 'SSH_CONNECTION' in os.environ
        if not in_ssh or (in_ssh and self.console.yesno(
                "Warning: Changing ip while an ssh session is active will"
                " drop said ssh session!", autosize=True) == self.OK):
            self.console.infobox(f"Requesting DHCP for {self.ifname}...")
            err = ifutil.set_dhcp(self.ifname)
            if err:
                self.console.msgbox("Error", err)

        return "ifconf"

    def _ifconf_default(self) -> str:
        conf.Conf().set_default_nic(self.ifname)
        return "ifconf"

    def _adv_install(self) -> str:
        text = "Please note that any changes you may have made to the\n"
        text += "live system will *not* be installed to the hard disk.\n\n"
        self.console.msgbox("Installer", text)

        self.installer.execute()
        return "advanced"

    def _shutdown(self, text: str, opt: str) -> str:
        if self.console.yesno(text) == self.OK:
            self.running = False
            cmd = f"shutdown {opt} now"
            fgvt = os.environ.get("FGVT")
            if fgvt:
                cmd = f"chvt {fgvt + cmd}; "
            os.system(cmd)

        return "advanced"

    def _adv_reboot(self) -> str:
        return self._shutdown("Reboot the appliance?", "-r")

    def _adv_shutdown(self):
        return self._shutdown("Shutdown the appliance?", "-h")

    def _adv_quit(self):
        if not self.advanced_enabled:
            self.running = False
            return "usage"

        if self.console.yesno("Do you really want to quit?",
                              autosize=True) == self.OK:
            self.running = False

        return "advanced"

    _adv_networking = networking
    quit = _adv_quit

    def loop(self, dialog: str = "usage") -> None:
        self.running = True
        prev_dialog = dialog
        standalone = dialog != 'usage'  # no "back" for plugins

        while dialog and self.running:
            try:
                if not dialog.startswith(PLUGIN_PATH):
                    try:
                        method = getattr(self, dialog)
                    except AttributeError:
                        raise Error("dialog not supported: " + dialog)
                else:
                    try:
                        method = self.pluginManager.path_map[dialog].run
                    except KeyError:
                        raise Error("could not find plugin dialog: " + dialog)

                new_dialog = method()
                if standalone:  # XXX This feels dirty
                    break
                prev_dialog = dialog
                dialog = new_dialog

            except Exception as e:
                sio = StringIO()
                traceback.print_exc(file=sio)

                self.console.msgbox("Caught exception", sio.getvalue())
                dialog = prev_dialog


def main():
    interactive = True
    advanced_enabled = True
    plugin_name = None

    if os.geteuid() != 0:
        fatal("confconsole needs root privileges to run")

    try:
        l_opts = ["help", "usage", "nointeractive", "plugin="]
        opts, args = getopt.gnu_getopt(sys.argv[1:], "hn", l_opts)
    except getopt.GetoptError as e:
        usage(e)

    for opt, val in opts:
        if opt in ("-h", "--help"):
            usage()
        elif opt == "--usage":
            advanced_enabled = False
        elif opt == "--nointeractive":
            interactive = False
        elif opt == "--plugin":
            plugin_name = val
        else:
            usage()

    em = plugin.EventManager()
    pm = plugin.PluginManager(PLUGIN_PATH,
                              {'eventManager': em, 'interactive': interactive})

    if plugin_name:

        ps = filter(lambda x: isinstance(x, plugin.Plugin),
                    pm.getByName(plugin_name))

        if len(ps) > 1:
            fatal(f'plugin name ambiguous, matches all of {ps}')
        elif len(ps) == 1:
            p = ps[0]

            if interactive:
                tc = TurnkeyConsole(pm, em, advanced_enabled)
                tc.loop(dialog=p.path)  # calls .run()
            else:
                p.module.run()
        else:
            fatal('no such plugin')
    else:
        tc = TurnkeyConsole(pm, em, advanced_enabled)
        tc.loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        subprocess.run(['stty', 'sane'])
        traceback.print_exc()
