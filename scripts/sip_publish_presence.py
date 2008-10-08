#!/usr/bin/env python

import sys
import re
import traceback
import string
import random
import socket
import os
import atexit
import select
import termios
import signal
from thread import start_new_thread, allocate_lock
from threading import Thread, Event
from Queue import Queue
from optparse import OptionParser, OptionValueError
from time import sleep
from collections import deque
import dns.resolver
from application.process import process
from application.configuration import *
from pypjua import *

from pypjua.applications import BuilderError
from pypjua.applications.pidf import *
from pypjua.applications.presdm import *
from pypjua.applications.rpid import *

from pypjua.clients.clientconfig import get_path

re_host_port = re.compile("^(?P<host>\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(:(?P<port>\d+))?$")
class SIPProxyAddress(tuple):
    def __new__(typ, value):
        match = re_host_port.search(value)
        if match is None:
            raise ValueError("invalid IP address/port: %r" % value)
        if match.group("port") is None:
            port = 5060
        else:
            port = match.group("port")
            if port > 65535:
                raise ValueError("port is out of range: %d" % port)
        return match.group("host"), port


class AccountConfig(ConfigSection):
    _datatypes = {"sip_address": str, "password": str, "display_name": str, "outbound_proxy": SIPProxyAddress}
    sip_address = None
    password = None
    display_name = None
    outbound_proxy = None, None


process._system_config_directory = os.path.expanduser("~/.sipclient")
configuration = ConfigFile("config.ini")


queue = Queue()
getstr_event = Event()
packet_count = 0
start_time = None
old = None
user_quit = True
lock = allocate_lock()
pub = None
sip_uri = None
string = None

pidf = None
person = None

menu_stack = deque()

def publish_pidf():
    try:
        pub.publish("application", "pidf+xml", pidf.toxml())
    except BuilderError, e:
        queue.put(("print", "PIDF as currently defined is invalid: %s" % str(e)))
    except:
        traceback.print_exc()
    else:
        queue.put(("print_frommenu", "PUBLISHing PIDF"))

class Menu(object):
    def __init__(self, interface):
        interface['x'] = {"description": "exit to upper level menu", "handler": Menu.exitMenu()}
        interface['q'] = {"description": "quit program", "handler": lambda: queue.put(("quit", None))}
        self.interface = interface
    
    def print_prompt(self):
        buf = ["Commands:"]
        for key, desc in self.interface.items():
            buf.append("  %s: %s" % (key, desc['description']))
        queue.put(("print_menu", "\n".join(buf)))

    def process_input(self, key):
        desc = self.interface.get(key)
        if desc is not None:
            desc["handler"]()
        else:
            queue.put(("print", "Illegal key"))

    def add_action(self, key, description):
        self.interface[key] = description

    def del_action(self, key):
        try:
            del self.interface[key]
        except KeyError:
            pass

    @staticmethod
    def gotoMenu(menu):
        func = (lambda: menu_stack.append(menu))
        func.menu = menu
        return func

    @staticmethod
    def exitMenu():
        return lambda: menu_stack.pop()


class NotesMenu(Menu):
    def __init__(self, obj=None, timestamp_type=None):
        Menu.__init__(self, {'s': {"description": "show current notes", "handler": self._show_notes},
                             'a': {"description": "add a note", "handler": self._add_note},
                             'd': {"description": "delete a note", "handler": self._del_note},
                             'c': {"description": "clear all note data", "handler": self._clear_notes}})
        self.list = NoteList()
        self.obj = obj
        self.timestamp_type = timestamp_type

    def _show_notes(self):
        buf = ["Notes:"]
        for note in self.list:
            buf.append(" %s'%s'" % ((note.lang is None) and ' ' or (' (%s) ' % note.lang), note.value))
        queue.put(("print_frommenu", '\n'.join(buf)))
    
    def _add_note(self):
        lang = getstr("Language")
        if lang == '':
            lang = None
        value = getstr("Note")
        self.list.append(Note(value, lang))
        if self.obj:
            self.obj.timestamp = self.timestamp_type()
        queue.put(("print_frommenu", "Note added"))

    def _del_note(self):
        buf = ["Current notes:"]
        for note in self.list:
            buf.append(" %s'%s'" % ((note.lang is None) and ' ' or (' (%s) ' % note.lang), note.value))
        print '\n'.join(buf)
        lang = getstr("\nLanguage of note to delete")
        if lang == '':
            lang = None
        try:
            del self.list[lang]
        except KeyError:
            queue.put(("print_frommenu", "No note in language `%s'" % lang))
        else:
            if self.obj:
                self.obj.timestamp = self.timestamp_type()
            queue.put(("print_frommenu", "Note deleted"))

    def _clear_notes(self):
        notes = list(self.list)
        for note in notes:
            del self.list[note.lang]
        if self.obj:
            self.obj.timestamp = self.timestamp_type()
        queue.put(("print_frommenu", "Notes deleted"))

# Mood manipulation pidf
class MoodMenu(Menu):
    def __init__(self):
        Menu.__init__(self, {'s': {"description": "show current moods", "handler": self._show_moods},
                             'a': {"description": "add a mood", "handler": self._add_mood},
                             'd': {"description": "delete a mood", "handler": self._del_mood},
                             'c': {"description": "clear all mood data", "handler": self._clear_moods},
                             'n': {"description": "handle mood notes", "handler": Menu.gotoMenu(NotesMenu(person, DMTimestamp))}})

    def _show_moods(self):
        buf = ["Moods:"]
        if person.mood is not None:
            for m in person.mood.values:
                buf.append("  %s" % str(m))
        queue.put(("print_frommenu", '\n'.join(buf)))
    
    def _add_mood(self):
        buf = ["Possible moods:"]
        values = list(Mood._xml_value_maps.get(value, value) for value in Mood._xml_values)
        values.sort()
        max_len = max(len(s) for s in values)+2
        format = " %%02d) %%-%ds" % max_len
        num_line = 72/(max_len+5)
        i = 0
        text = ''
        for val in values:
            text += format % (i+1, val)
            i += 1
            if i % num_line == 0:
                buf.append(text)
                text = ''
        print '\n'.join(buf)
        m = getstr("\nSelect mood to add")
        try:
            m = int(m)
        except ValueError:
            queue.put(("print_frommenu", "Invalid input"))
        else:
            if person.mood is None:
                person.mood = Mood()
                person.mood.notes = self.interface['n']['handler'].menu.list
            person.mood.add(values[m-1])
            person.timestamp = DMTimestamp()
            queue.put(("print_frommenu", "Mood added"))

    def _del_mood(self):
        if person.mood is None:
            queue.put(("print_frommenu", "There is no current mood set"))
            return
        buf = ["Current moods:"]
        values = person.mood.values
        values.sort()
        max_len = max(len(s) for s in values)+2
        format = " %%02d) %%-%ds" % max_len
        num_line = 72/(max_len+5)
        i = 0
        text = ''
        for val in values:
            text += format % (i+1, val)
            i += 1
            if i % num_line == 0:
                buf.append(text)
                text = ''
        buf.append(text)
        print '\n'.join(buf)
        m = getstr("\nSelect mood to delete")
        try:
            m = int(m)
        except ValueError:
            queue.put(("print_frommenu", "Invalid input"))
        else:
            person.mood.remove(values[m-1])
            person.timestamp = DMTimestamp()
            queue.put(("print_frommenu", "Mood deleted"))

    def _clear_moods(self):
        if person.mood is None:
            queue.put(("print_frommenu", "There is no current mood set"))
            return
        person.mood = None
        person.timestamp = DMTimestamp()
        queue.put(("print_frommenu", "Mood information cleared"))


def termios_restore():
    global old
    if old is not None:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)

atexit.register(termios_restore)

def getstr(prompt='selection'):
    global string, getstr_event
    string = ''
    sys.stdout.write("%s> " % prompt)
    sys.stdout.flush()
    getstr_event.wait()
    getstr_event.clear()
    sys.stdout.write("\n")
    ret = string
    string = None
    return ret

def getchar():
    global old
    fd = sys.stdin.fileno()
    if os.isatty(fd):
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] = new[3] & ~termios.ICANON & ~termios.ECHO
        new[6][termios.VMIN] = '\000'
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
            if select.select([fd], [], [], None)[0]:
                return sys.stdin.read(10)
        finally:
            termios_restore()
    else:
        return os.read(fd, 10)

def event_handler(event_name, **kwargs):
    global packet_count, start_time, queue, pjsip_logging
    if event_name == "Publication_state":
        if kwargs["state"] == "unpublished":
            queue.put(("print", "Unpublished: %(code)d %(reason)s" % kwargs))
            queue.put(("quit", None))
        elif kwargs["state"] == "published":
            queue.put(("print", "PUBLISH was successful"))
    elif event_name == "siptrace":
        if start_time is None:
            start_time = kwargs["timestamp"]
        packet_count += 1
        if kwargs["received"]:
            direction = "RECEIVED"
        else:
            direction = "SENDING"
        buf = ["%s: Packet %d, +%s" % (direction, packet_count, (kwargs["timestamp"] - start_time))]
        buf.append("%(timestamp)s: %(source_ip)s:%(source_port)d --> %(destination_ip)s:%(destination_port)d" % kwargs)
        buf.append(kwargs["data"])
        queue.put(("print", "\n".join(buf)))
    elif event_name != "log":
        queue.put(("pypjua_event", (event_name, kwargs)))
    elif pjsip_logging:
        queue.put(("print", "%(timestamp)s (%(level)d) %(sender)14s: %(message)s" % kwargs))

def read_queue(e, username, domain, password, display_name, proxy_ip, proxy_port, expires, do_siptrace, pjsip_logging):
    global user_quit, lock, queue, pub, sip_uri, pidf, person
    lock.acquire()
    try:
        if proxy_ip is None:
            # for now assume 1 SRV record and more than one A record
            srv_answers = dns.resolver.query("_sip._udp.%s" % domain, "SRV")
            a_answers = dns.resolver.query(str(srv_answers[0].target), "A")
            route = Route(random.choice(a_answers).address, srv_answers[0].port)
        else:
            route = Route(proxy_ip, proxy_port)
        sip_uri = SIPURI(user=username, host=domain, display=display_name)
        pub = Publication(Credentials(sip_uri, password), "presence", route=route, expires=expires)
        
        # initialize PIDF
        pidf = PIDF(entity='%s@%s' % (username, domain))
        
        person = Person(''.join(chr(random.randint(97, 122)) for i in xrange(8)))
        person.timestamp = DMTimestamp()
        pidf.append(person)

        # initialize menus
        top_level = Menu({'s': {"description": "show PIDF", "handler": lambda: queue.put(("print_frommenu", pidf.toxml(pretty_print=True)))},
                          'p': {"description": "publish PIDF", "handler": publish_pidf}})
        top_level.del_action('x')
        menu_stack.append(top_level)

        top_level.add_action('m', {"description": "set mood information", "handler": Menu.gotoMenu(MoodMenu())})
        person_notes_menu = NotesMenu(person, DMTimestamp)
        top_level.add_action('n', {"description": "handle notes", "handler": Menu.gotoMenu(person_notes_menu)})
        
        # stuff that depends on menus
        person.notes = person_notes_menu.list
        
        menu_stack[-1].print_prompt()
        while True:
            command, data = queue.get()
            if command == "print":
                print data
                menu_stack[-1].print_prompt()
            if command == "print_menu":
                print
                print 'Identity: %s@%s' % (sip_uri.user, sip_uri.host)
                print data
                print
            if command == "print_frommenu":
                print data
            if command == "pypjua_event":
                event_name, args = data
            if command == "user_input":
                key = data
            if command == "eof":
                command = "end"
                want_quit = True
            if command == "end":
                try:
                    pub.unpublish()
                except:
                    pass
            if command == "quit":
                user_quit = False
                break
            if command == "user_input":
                menu_stack[-1].process_input(data)
                menu_stack[-1].print_prompt()
    except:
        user_quit = False
        traceback.print_exc()
    finally:
        e.stop()
        if not user_quit:
            os.kill(os.getpid(), signal.SIGINT)
        lock.release()

def do_publish(**kwargs):
    global user_quit, lock, queue, pjsip_logging, string, getstr_event, old
    ctrl_d_pressed = False
    pjsip_logging = kwargs["pjsip_logging"]

    e = Engine(event_handler, do_siptrace=kwargs['do_siptrace'], auto_sound=False)
    e.start()
    start_new_thread(read_queue, (e,), kwargs)
    atexit.register(termios_restore)
    
    try:
        while True:
            char = getchar()
            if char == "\x04":
                if not ctrl_d_pressed:
                    queue.put(("eof", None))
                    ctrl_d_pressed = True
            else:
                if string is not None:
                    if char == "\x7f":
                        if len(string) > 0:
                            char = "\x08"
                            sys.stdout.write("\x08 \x08")
                            sys.stdout.flush()
                            string = string[:-1]
                    else:
                        if old is not None:
                            sys.stdout.write(char)
                            sys.stdout.flush()
                        if char == "\x0A":
                            getstr_event.set()
                        else:
                            string += char
                else:
                    queue.put(("user_input", char))
    except KeyboardInterrupt:
        if user_quit:
            print "Ctrl+C pressed, exiting instantly!"
            queue.put(("quit", True))
        return

def parse_host_port(option, opt_str, value, parser, host_name, port_name, default_port):
    match = re_host_port.match(value)
    if match is None:
        raise OptionValueError("Could not parse supplied address: %s" % value)
    setattr(parser.values, host_name, match.group("host"))
    if match.group("port") is None:
        setattr(parser.values, port_name, default_port)
    else:
        setattr(parser.values, port_name, int(match.group("port")))

def parse_options():
    retval = {}
    description = "This example script will publish the rich presence state of the specified SIP account based on a menu-driven interface."
    usage = "%prog [options]"
    parser = OptionParser(usage=usage, description=description)
    parser.print_usage = parser.print_help
    parser.add_option("-a", "--account-name", type="string", dest="account_name", help="The account name from which to read account settings. Corresponds to section Account_NAME in the configuration file. If not supplied, the section Account will be read.", metavar="NAME")
    parser.add_option("--sip-address", type="string", dest="sip_address", help="SIP address of the user in the form user@domain")
    parser.add_option("-e", "--expires", type="int", dest="expires", help='"Expires" value to set in PUBLISH. Default is 300 seconds.')
    parser.add_option("-o", "--outbound-proxy", type="string", action="callback", callback=lambda option, opt_str, value, parser: parse_host_port(option, opt_str, value, parser, "proxy_ip", "proxy_port", 5060), help="Outbound SIP proxy to use. By default a lookup is performed based on SRV and A records. This overrides the setting from the config file.", metavar="IP[:PORT]")
    parser.add_option("-s", "--trace-sip", action="store_true", dest="do_siptrace", help="Dump the raw contents of incoming and outgoing SIP messages (disabled by default).")
    parser.add_option("-l", "--log-pjsip", action="store_true", dest="pjsip_logging", help="Print PJSIP logging output (disabled by default).")
    options, args = parser.parse_args()
    
    if options.account_name is None:
        account_section = "Account"
    else:
        account_section = "Account_%s" % options.account_name
    configuration.read_settings(account_section, AccountConfig)
    default_options = dict(expires=300, proxy_ip=AccountConfig.outbound_proxy[0], proxy_port=AccountConfig.outbound_proxy[1], sip_address=AccountConfig.sip_address, password=AccountConfig.password, display_name=AccountConfig.display_name, do_siptrace=False, pjsip_logging=False)
    options._update_loose(dict((name, value) for name, value in default_options.items() if getattr(options, name, None) is None))
    
    if not all([options.sip_address, options.password]):
        raise RuntimeError("No complete set of SIP credentials specified in config file and on commandline.")
    for attr in default_options:
        retval[attr] = getattr(options, attr)
    try:
        retval["username"], retval["domain"] = options.sip_address.split("@")
    except ValueError:
        raise RuntimeError("Invalid value for sip_address: %s" % options.sip_address)
    else:
        del retval["sip_address"]
    
    if options.account_name is None:
        print "Using default account: %s" % options.sip_address
    else:
        print "Using account '%s': %s" % (options.account_name, options.sip_address)
    accounts = ((acc == 'Account') and 'default' or "'%s'" % acc[8:] for acc in configuration.parser.sections() if acc.startswith('Account'))
    print "Accounts available: %s" % ', '.join(accounts)
    
    return retval

def main():
    do_publish(**parse_options())

if __name__ == "__main__":
    main()
