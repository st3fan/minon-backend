# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.


import collections
import logging
import os
import re
import time
import sys

from twisted.internet.task import LoopingCall

import requests

import minion.curly
from minion.plugins.base import AbstractPlugin,BlockingPlugin,ExternalProcessPlugin

#
# AlivePlugin
#

class AlivePlugin(BlockingPlugin):

    """
    This plugin checks if the site is alive or not. If any error occurs, the whole plan
    will be aborted. This is useful to have as the first plugin in a workflow. Anything
    non-200 will be seen as a fatal error.
    """

    PLUGIN_NAME = "Alive"
    PLUGIN_WEIGHT = "light"

    def do_run(self):
        try:
            r = minion.curly.get(self.configuration['target'], connect_timeout=5, timeout=15)
            r.raise_for_status()
        except Exception as e:
            issue = { "Summary":"Site could not be reached",
                      "Severity":"Error",
                      "URLs": [ { "URL": self.configuration['target'], "Extra": str(e) } ] }
            self.report_issues([issue])

#
# XFrameOptionsPlugin
#

class XFrameOptionsPlugin(BlockingPlugin):

    """
    This is a minimal plugin that does one http request to find out if
    the X-Frame-Options header has been set. It does not override anything
    except start() since that one check is quick and there is no point
    in suspending/resuming/terminating.

    All plugins run in a separate process so we can safely do a blocking
    HTTP request. The PluginRunner catches exceptions thrown by start() and
    will report that back as an error state of the plugin.
    """

    PLUGIN_NAME = "XFrameOptions"
    PLUGIN_WEIGHT = "light"

    def do_run(self):
        r = requests.get(self.configuration['target'], timeout=5.0)
        r.raise_for_status()
        if 'x-frame-options' in r.headers:
            if r.headers['x-frame-options'].upper() not in ('DENY', 'SAMEORIGIN'):
                self.report_issues([{ "Summary":"Site has X-Frame-Options header but it has an unknown or invalid value: %s" % r.headers['x-frame-options'],"Severity":"High" }])
            else:
                self.report_issues([{ "Summary":"Site has a correct X-Frame-Options header", "Severity":"Info" }])
        else:
            self.report_issues([{"Summary":"Site has no X-Frame-Options header set", "Severity":"High"}])


class HSTSPlugin(BlockingPlugin):

    """
    This plugin checks if the site sends out an HSTS header if it is HTTPS enabled.
    """

    PLUGIN_NAME = "HSTS"
    PLUGIN_WEIGHT = "light"

    def do_run(self):
        r = requests.get(self.configuration['target'], timeout=5.0)
        r.raise_for_status()
        if r.url.startswith("https://"):
            if 'strict-transport-security' not in r.headers:
                self.report_issues([{ "Summary":"Site does not set Strict-Transport-Security header", "Severity":"High" }])
            else:
                self.report_issues([{ "Summary":"Site sets Strict-Transport-Security header", "Severity":"Info" }])


class XContentTypeOptionsPlugin(BlockingPlugin):

    """
    This plugin checks if the site sends out a X-Content-Type-Options header
    """

    PLUGIN_NAME = "XContentTypeOptions"
    PLUGIN_WEIGHT = "light"

    def do_run(self):
        r = requests.get(self.configuration['target'], timeout=5.0)
        r.raise_for_status()
        if 'X-Content-Type-Options' not in r.headers:
            self.report_issues([{ "Summary":"Site does not set X-Content-Type-Options header", "Severity":"High" }])
        else:
            if r.headers['X-Content-Type-Options'] == 'nosniff':
                self.report_issues([{ "Summary":"Site sets X-Content-Type-Options header", "Severity":"Info" }])
            else:
                self.report_issues([{ "Summary":"Site sets an invalid X-Content-Type-Options header", "Severity":"High" }])


class XXSSProtectionPlugin(BlockingPlugin):

    """
    This plugin checks if the site sends out a X-XSS-Protection header
    """

    PLUGIN_NAME = "XXSSProtection"
    PLUGIN_WEIGHT = "light"

    def do_run(self):
        r = requests.get(self.configuration['target'], timeout=5.0)
        r.raise_for_status()
        if 'X-XSS-Protection' not in r.headers:
            self.report_issues([{ "Summary":"Site does not set X-XSS-Protection header", "Severity":"High" }])
        else:
            if r.headers['X-XSS-Protection'] == '1; mode=block':
                self.report_issues([{ "Summary":"Site sets X-XSS-Protection header", "Severity":"Info" }])
            elif r.headers['X-XSS-Protection'] == '0':
                self.report_issues([{ "Summary":"Site sets X-XSS-Protection header to disable the XSS filter", "Severity":"High" }])
            else:
                self.report_issues([{ "Summary":"Site sets an invalid X-XSS-Protection header: %s" % r.headers['X-XSS-Protection'], "Severity":"High" }])


class ServerDetailsPlugin(BlockingPlugin):

    """
    This plugin checks if the site sends out a Server or X-Powered-By header that exposes details about the server software.
    """
    
    PLUGIN_NAME = "ServerDetails"
    PLUGIN_WEIGHT = "light"

    def do_run(self):
        r = requests.get(self.configuration['target'], timeout=5.0)
        r.raise_for_status()
        HEADERS = ('Server', 'X-Powered-By', 'X-AspNet-Version', 'X-AspNetMvc-Version', 'X-Backend-Server')
        for header in HEADERS:
            if header in r.headers:
                self.report_issues([{ "Summary":"Site sets the '%s' header" % header, "Severity":"Medium" }])


class RobotsPlugin(BlockingPlugin):
    
    """
    This plugin checks if the site has a robots.txt.
    """

    PLUGIN_NAME = "Robots"
    PLUGIN_WEIGHT = "light"

    def do_run(self):
        r = requests.get(self.configuration['target'], timeout=5.0)
        if r.status_code != 200:
            self.report_issues([{"Summary":"No robots.txt found", "Severity": "Medium"}])

#
# CSPPlugin
#        

def _parse_csp(csp):
    options = collections.defaultdict(list)
    p = re.compile(r';\s*')
    for rule in p.split(csp):
        a = rule.split()
        options[a[0]] += a[1:]
    return options

class CSPPlugin(BlockingPlugin):

    """
    This plugin checks if a CSP header is set.
    """

    PLUGIN_NAME = "CSP"
    PLUGIN_WEIGHT = "light"

    def do_run(self):

        r = minion.curly.get(self.configuration['target'], connect_timeout=5, timeout=15)
        r.raise_for_status()

        # Fast fail if both headers are set
        if 'x-xontent-security-policy' in r.headers and 'x-content-security-policy-report-only' in r.headers:
            self.report_issues([{"Summary":"Both X-Content-Security-Policy and X-Content-Security-Policy-Report-Only headers set", "Severity": "High"}])
            return

        # Fast fail if only reporting is enabled
        if 'x-content-security-policy-report-only' in r.headers:
            self.report_issues([{"Summary":"X-Content-Security-Policy-Report-Only header set", "Severity": "High"}])
            return

        # Fast fail if no CSP header is set
        if 'x-content-security-policy' not in r.headers:
            self.report_issues([{"Summary":"No X-Content-Security-Policy header set", "Severity": "High"}])
            return

        # Parse the CSP and look for issues
        csp_config = _parse_csp(r.headers["x-content-security-policy"])
        if not csp_config:
            self.report_issues([{"Summary":"Malformed X-Content-Security-Policy header set", "Severity":"High"}])
            return
            
        # Allowing eval-script or inline-script defeats the purpose of CSP?
        if 'eval-script' in csp_config['options']:
            self.report_issues([{"Summary":"CSP Rules allow eval-script", "Severity":"High"}])
        if 'inline-script' in csp_config['options']:
            self.report_issues([{"Summary":"CSP Rules allow inline-script", "Severity":"High"}])
