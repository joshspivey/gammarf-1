#!/usr/bin/env python2
# spectrum module
#
# Joshua Davis (gammarf -*- covert.codes)
# http://gammarf.io
# Copyright(C) 2017
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.


from __future__ import division

import abc
import datetime
import json
import math
import os
import socket
import threading
import time
from collections import OrderedDict
from hashlib import md5
from multiprocessing import Process, Queue
from subprocess import Popen, PIPE, STDOUT
from sys import builtin_module_names

from gammarf_base import GrfModuleBase

AVG_SAMPLES = 50  # how many samples to avg before sending data
CHECK_QUEUE_INTERVAL = 10000
CMD_BUFSZ = 32768
CMDSOCK_TIMEOUT = 10
ERROR_SLEEP = 3
HACKRF_LNA_GAIN = 32
HACKRF_MIN_SCAN_MHZ = 40
HACKRF_MAX_SCAN_MHZ = 2000
MODULE_DESCRIPTION = "spectrum module"
REPORTER_QSIZE_LIMIT = 30e6
REPORTER_SLEEP = 1/500  # limit data rate to server
RTLSDR_CROP = 20  # %
RTLSDR_INTEGRATION_INTERVAL = 3
RTLSDR_WINDOW = "hamming"
THREAD_TIMEOUT = 3

device_list = ["rtlsdr", "hackrf"]


def start(config):
    return GrfModuleSpectrum(config)


class Reporter(Process):
    def __init__(self, reporter_opts, reporter_queue, settings):
        self.station_id = reporter_opts['station_id']
        self.station_pass = reporter_opts['station_pass']
        self.server_host = reporter_opts['server_host']
        self.server_port = reporter_opts['server_port']
        self.freqmap = dict()
        self.running = True
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.reporter_queue = reporter_queue

        self.settings = dict()
        for setting, default in settings.items():
            self.settings[setting] = default

        super(Reporter, self).__init__()

    def run(self):
        while self.running:
            (op, args) = self.reporter_queue.get()

            if op == "stop":
                self.running = False
                continue

            if op == "toggle":
                self.settings[args[0]] = args[1]
                continue

            freq, pwr, loc, ct, step = args
            sent = False

            try:
                fent = self.freqmap[freq]
            except KeyError:
                fent = {'mean': 0, 'stdev': 0, 'n': 0, 'S': 0, 'last_reported_mean': None, 'last_reported_stdev': None}

            # http://dsp.stackexchange.com/questions/811/determining-the-mean-and-standard-deviation-in-real-time
            prev_mean = fent['mean']
            fent['n'] = fent['n'] + 1
            fent['mean'] = fent['mean'] + (pwr - fent['mean']) / fent['n']
            fent['S'] = fent['S'] + (pwr - fent['mean']) * (pwr - prev_mean)
            fent['stdev'] = math.sqrt(fent['S']/fent['n'])

            if fent['n'] > AVG_SAMPLES:
                if fent['last_reported_mean']:
                    if abs(fent['mean']) > abs(fent['last_reported_mean']) + fent['last_reported_stdev']:
                        self.send_stats(freq, fent['mean'], fent['stdev'])
                        fent['last_reported_mean'] = fent['mean']
                        fent['last_reported_stdev'] = fent['stdev']
                        sent = True
                else:
                    self.send_stats(freq, fent['mean'], fent['stdev'])
                    fent['last_reported_mean'] = fent['mean']
                    fent['last_reported_stdev'] = fent['stdev']
                    sent = True

            self.freqmap[freq] = fent

            if sent:
                time.sleep(REPORTER_SLEEP)

        return

    def send_stats(self, freq, mean, stdev):
        data = OrderedDict()
        data['freq'] = freq
        data['mean'] = str(mean)
        data['stdev'] = str(stdev)
        data['ct'] = int(time.time())
        data['stationid'] = self.station_id
        data['module'] = 'sp'

        # just basic sanity
        m = md5()
        m.update(self.station_pass + str(data['mean']) + str(data['ct']))
        data['sign'] = m.hexdigest()[:12]

        self.socket.sendto(json.dumps(data), (self.server_host, self.server_port))


class Spectrum(threading.Thread):
    def __init__(self, spectrum_opts, reporter, reporter_queue, gpsp, devmod, parent):
        threading.Thread.__init__(self)

        self.devnum = spectrum_opts['devnum']
        self.sdrtype = spectrum_opts['sdrtype']
        self.reporter = reporter
        self.reporter_queue = reporter_queue
        self.gpsp = gpsp
        self.devmod = devmod
        self.parent = parent
        self.overflow = False

        cmd = spectrum_opts['cmd']
        fstr = spectrum_opts['fstr']

        ON_POSIX = 'posix' in builtin_module_names 

        if self.sdrtype == 'rtlsdr':
            integration = spectrum_opts['integration']
            gain = spectrum_opts['gain']
            ppm = spectrum_opts['ppm']

            self.cmdpipe = Popen([cmd, "-d {}".format(self.devnum), "-f {}".format(fstr),
                    "-i {}".format(integration), "-p {}".format(ppm), "-g {}".format(gain),
                    "-c {}%".format(RTLSDR_CROP), "-w {}".format(RTLSDR_WINDOW)],
                    stdout=PIPE, stderr=STDOUT, close_fds=ON_POSIX)

        elif self.sdrtype == 'hackrf':
            width = spectrum_opts['width']
            lna_gain = spectrum_opts['lna_gain']
            vga_gain = spectrum_opts['vga_gain']

            DEVNULL = open(os.devnull, 'w')

            self.cmdpipe = Popen([cmd, "-f{}".format(fstr), "-w {}".format(width),
                "-l {}".format(lna_gain), "-g {}".format(vga_gain)],
                stdout=PIPE, stderr=DEVNULL, close_fds=ON_POSIX)

        self.stoprequest = threading.Event()

    def run(self):
        check_q = 0

        while not self.stoprequest.isSet():
            data = self.cmdpipe.stdout.readline()

            if len(data) == 0:
                try:
                    continue
                except:
                    return

            # look for gps here to avoid flooding the reporter in the case of no lock
            loc = self.gpsp.get_current()
            if (loc == None) or (loc['lat'] == "0.0" and loc['lng'] == "0.0") or (loc['lat'] == "NaN"):
                print("[spectrum] No GPS loc, waiting...")
                time.sleep(ERROR_SLEEP)
                continue

            for raw in data.split('\n'):
                if len(raw) == 0:
                    continue

                if self.sdrtype == 'rtlsdr':
                    if len(raw.split(' ')[0].split('-')) != 3:  # line irrelevant, or from stderr
                        if raw == "Error: dropped samples.":
                            print("[spectrum] Error with device {}, exiting task".format(self.devnum))
                            self.devmod.removedev(self.devnum)
                            return
                        continue

                try:
                    _date, _time, freq_low, freq_high, step, _samples, raw_readings = raw.split(', ', 6)
                    freq_low = float(freq_low)
                    step = float(step)
                    readings = [x.strip() for x in raw_readings.split(',')]

                except Exception:
                    print("[spectrum] Thread exiting on exception")
                    return

                ct = int(time.time())
                for i in range(len(readings)):
                    freq = int(round(freq_low + (step * i)))
                    pwr = float(readings[i])
                    try:
                        self.reporter_queue.put( ("data", (freq, pwr, loc, ct, step) ) )
                    except AssertionError:  # can happen while exiting
                        continue

            check_q += 1
            if check_q >= CHECK_QUEUE_INTERVAL:
                qsize = self.reporter_queue.qsize()
                if qsize > REPORTER_QSIZE_LIMIT:
                    print("[spectrum] WARINING: Queue filling up too fast!  Run spectrum over less bandwidth.")
                    self.overflow = True
                    break
                check_q = 0

        self.cmdpipe.stdout.close()
        self.cmdpipe.kill()
        os.kill(self.cmdpipe.pid, 9)
        os.wait()

        if self.overflow:
            self.parent.stop(self.devnum, self.devmod)

        return

    def join(self, timeout=None):
        self.stoprequest.set()
        super(Spectrum, self).join(timeout)


class GrfModuleSpectrum(GrfModuleBase):
    def __init__(self, config):
        rtl_path = config.rtldevs.rtl_path
        if not isinstance(rtl_path, str) or not rtl_path:
            raise Exception("param 'rtl_path' not appropriately defined in config")

        rtlcmd = rtl_path + '/' + 'rtl_power'
        if not os.path.isfile(rtlcmd) or not os.access(rtlcmd, os.X_OK):
            raise Exception("executable rtl_power not found in specified path")

        hackrf_path = config.hackrfdevs.hackrf_path
        if not isinstance(hackrf_path, str) or not hackrf_path:
            raise Exception("param 'hackrf_path' not appropriately defined in config")

        hackrfcmd = hackrf_path + '/' + 'hackrf_sweep'
        if not os.path.isfile(hackrfcmd) or not os.access(hackrfcmd, os.X_OK):
            raise Exception("executable hackrf_sweep not found in specified path")

        self.rtlcmd = rtlcmd
        self.hackrfcmd = hackrfcmd
        self.spectrum_workers = list()
        self.reporter = None

        self.settings = {}

        print("Loading {}".format(MODULE_DESCRIPTION))

    def help(self):
        print("Spectrum: Report statistics about large swaths of bandwidth")
        print("")
        print("Usage: spectrum devnum freqs")
        print("\tWhere freqs is a frequency range in rtl_power format.")
        print("\tExample: > run spectrum 0 200M:300M:15k")
        print("")
        print("\tSettings:")
        return True

    def run(self, devnum, freqs, system_params, loadedmods, remotetask=False):
        self.remotetask = remotetask
        devmod = loadedmods['devices']

        # these things are done only once
        if not self.reporter:  # don't do in init -- what if the module's never used?
            reporter_opts = {'station_id': system_params['station_id'],
                    'station_pass': system_params['station_pass'],
                    'server_host': system_params['server_host'],
                    'server_port': system_params['server_port']}

            self.gpsworker = loadedmods['location'].gps_worker
            self.reporter_queue = Queue()
            reporter = Reporter(reporter_opts, self.reporter_queue, self.settings)
            reporter.daemon = True
            reporter.start()
            self.reporter = reporter

        if not freqs:
            print("Must include a frequency specification")
            return
        freqs = freqs.strip()
        if len(freqs.split(':')) != 3:
            print("Bad frequency specification")
            return

        devtype = devmod.get_devtype(devnum)
        if devtype == "rtlsdr":
            try:
                outfreqs = []
                lowfreq, highfreq, width = freqs.split(':')
                for f in [lowfreq, highfreq]:
                    if f[len(f)-1] == 'M':
                        f = int(float(f[:len(f)-1])*1e6)
                    elif f[len(f)-1] == 'k':
                        f = int(float(f[:len(f)-1])*1e3)
                    else:
                        f = int(f)
                    outfreqs.append(f)
            except Exception:
                print("Error parsing frequency string")
                return

            if outfreqs[1] < outfreqs[0]:
                print("Second scan frequency must be greater than the first scan frequency")
                return

            fstr = "{}:{}:{}".format(str(outfreqs[0]), str(outfreqs[1]), width)

            spectrum_opts = {'cmd': self.rtlcmd,
                    'devnum': devnum,
                    'fstr': fstr,
                    'integration': RTLSDR_INTEGRATION_INTERVAL,
                    'ppm': devmod.get_ppm(devnum),
                    'gain': devmod.get_gain(devnum),
                    'sdrtype': 'rtlsdr'}

        elif devtype == "hackrf":
            try:
                outfreqs = []
                lowfreq, highfreq, width = freqs.split(':')
                for f in [lowfreq, highfreq]:
                    if f[len(f)-1] == 'M':
                        f = int(float(f[:len(f)-1]))
                    elif f[len(f)-1] == 'k':
                        print("Use frequencies with an 'M' (MHz) suffix")
                        return
                    else:
                        print("Use frequencies with an 'M' (MHz) suffix")
                        return
                    outfreqs.append(f)

                if outfreqs[1] < outfreqs[0]:
                    print("Second scan frequency must be greater than the first scan frequency")
                    return

                if outfreqs[1] - outfreqs[0] < HACKRF_MIN_SCAN_MHZ:
                    print("You must scan at least {} MHz when using HackRF.".format(HACKRF_MIN_SCAN_MHZ))
                    return

                if outfreqs[1] - outfreqs[0] > HACKRF_MAX_SCAN_MHZ:
                    print("You may scan at most {} MHz when using HackRF.".format(HACKRF_MAX_SCAN_MHZ))
                    return

                if width[len(width)-1] == 'M':
                    width = int(float(width[:len(width)-1]) * 1e6)
                elif width[len(width)-1] == 'k':
                    width = int(float(width[:len(width)-1]) * 1e3)
                else:
                    width = int(width)

            except Exception:
                print("Error parsing frequency string")
                return

            fstr = "{}:{}".format(str(outfreqs[0]), str(outfreqs[1]))

            spectrum_opts = {'cmd': self.hackrfcmd,
                    'devnum': devnum,
                    'fstr': fstr,
                    'width': width,
                    'lna_gain': devmod.get_lna_gain(devnum),
                    'vga_gain': devmod.get_vga_gain(devnum),
                    'sdrtype': 'hackrf'}

        spectrum = Spectrum(spectrum_opts, self.reporter, self.reporter_queue, self.gpsworker, devmod, self)
        spectrum.daemon = True
        spectrum.start()
        self.spectrum_workers.append( (devnum, spectrum) )

        print("Spectrum added on device {}".format(devnum))
        print("[spectrum] NOTE: It takes awhile to gather samples to form an average, for new frequency ranges")

        return True

    def report(self):
        return

    def info(self):
        return

    def shutdown(self):
        print("Shutting down spectrum module(s)")
        try:  # if this module has not yet been run, this will cause an error
            self.reporter_queue.put( ("stop", (None) ) )
        except:
            pass

        self.reporter.terminate()
        self.reporter.join()
        self.reporter_queue.close()

        for spectrum in self.spectrum_workers:
            devnum, thread = spectrum
            thread.join(THREAD_TIMEOUT)

        return

    def showconfig(self):
        return

    def setting(self, setting, arg=None):
        if not self.reporter_queue:
            print("Module not ready")
            return True

        if setting == None:
            for setting, state in self.settings.items():
                print("{}: {} ({})".format(setting, state, type(state)))
            return True

        if setting == 0:
            return self.settings.keys()

        if setting not in self.settings.keys():
            return False

        if isinstance(self.settings[setting], bool):
            new = not self.settings[setting]
        elif not arg:
            print("Non-boolean setting requires an argument")
            return True
        else:
            if isinstance(self.settings[setting], int):
                new = int(arg)
            elif isinstance(self.settings[setting], float):
                new = float(arg)
            else:
                new = arg

        self.settings[setting] = new
        self.reporter_queue.put( ("toggle", (setting, new) ) )

        return True

    def stop(self, devnum, devmod):
        for spectrum in self.spectrum_workers:
            spectrum_devnum, thread = spectrum
            if spectrum_devnum == devnum:
                try:
                    thread.join(THREAD_TIMEOUT)
                except:  # exiting due to queue overflow
                    pass

                if not self.remotetask:
                    devmod.freedev(devnum)

                self.spectrum_workers.remove( (spectrum_devnum, thread) )
                return True

        return False

    def ispseudo(self):
        return False

    def devices(self):
        return device_list
