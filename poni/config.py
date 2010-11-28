"""
config rendering and verification

Copyright (c) 2010 Mika Eloranta
See LICENSE for details.

"""

import itertools
import datetime
import logging
import difflib
from . import errors
from . import util

import Cheetah.Template
from Cheetah.Template import Template as CheetahTemplate


class Manager:
    def __init__(self, confman):
        self.log = logging.getLogger("manager")
        self.files = []
        self.error_count = 0
        self.confman = confman
        self.dynamic_conf = []
        self.audit_format = "%8s %s: %s"

    def add_dynamic(self, item):
        self.dynamic_conf.append(item)

    def emit_error(self, node, source_path, error):
        self.log.warning("node %s: %s: %s: %s", node.name, source_path,
                         error.__class__.__name__, error)
        self.error_count += 1

    def verify(self, show=False, deploy=False, audit=False, show_diff=False,
               verbose=False, callback=None):
        self.log.debug("verify: %s", dict(show=show, deploy=deploy,
                                          audit=audit, show_diff=show_diff,
                                          verbose=verbose, callback=callback))
        files = [f for f in self.files if not f.get("report")]
        reports = [f for f in self.files if f.get("report")]
        for entry in itertools.chain(files, reports):
            if not entry["node"].verify_enabled():
                self.log.debug("filtered: verify disabled: %r", entry)
                continue

            if callback and not callback(entry):
                self.log.debug("filtered: callback: %r", entry)
                continue

            self.log.debug("verify: %r", entry)
            render = entry["render"]
            dest_path = entry["dest_path"]
            failed = False
            node_name = entry["node"].name

            source_path = entry["config"].path / entry["source_path"]
            try:
                dest_path, output = render(source_path, entry["dest_path"])
            except Exception, error:
                self.emit_error(entry["node"], source_path, error)
                output = util.format_error(error)
                failed = True

            if show:
                identity = "%s: dest=%s" % (node_name, dest_path)
                print "--- BEGIN %s ---" % identity
                print output
                print "--- END %s ---" % identity
                print

            remote = None

            if (audit or deploy) and dest_path and (not failed):
                # read existing file
                try:
                    remote = entry["node"].get_remote()
                    active_text = remote.read_file(dest_path)
                    stat = remote.stat(dest_path)
                    if stat:
                        active_time = datetime.datetime.fromtimestamp(
                            stat.st_mtime)
                    else:
                        active_time = ""
                except errors.RemoteError, error:
                    if audit:
                        self.log.error("%s: %s: %s: %s", node_name, dest_path,
                                       error.__class__.__name__, error)
                    active_text = None
            else:
                active_text = None

            if active_text and audit:
                self.audit_output(entry, dest_path, active_text, active_time,
                                  output, show_diff=show_diff)

            if deploy and dest_path and (not failed):
                remote = entry["node"].get_remote()
                try:
                    self.deploy_file(remote, entry, dest_path, output,
                                     active_text, verbose=verbose,
                                     mode=entry.get("mode"))
                except errors.RemoteError, error:
                    self.log.error("%s: %s: %s", node_name, dest_path, error)
                    # NOTE: continuing

    def deploy_file(self, remote, entry, dest_path, output, active_text,
                    verbose=False, mode=None):
        if output == active_text:
            # nothing to do
            if verbose:
                self.log.info(self.audit_format, "OK",
                              entry["node"].name, dest_path)

            return

        remote.write_file(dest_path, output, mode=mode)
        post_process = entry.get("post_process")
        if post_process:
            # TODO: remote support
            post_process(dest_path)

        self.log.info(self.audit_format, "WROTE",
                      entry["node"].name, dest_path)

    def audit_output(self, entry, dest_path, active_text, active_time,
                     output, show_diff=False):
        if (active_text is not None) and (active_text != output):
            self.log.warning(self.audit_format, "DIFFERS",
                             entry["node"].name, dest_path)
            if show_diff:
                diff = difflib.unified_diff(
                    output.splitlines(True),
                    active_text.splitlines(True),
                    "config", "active",
                    "TODO:mtime", active_time,
                    lineterm="\n")

                for line in diff:
                    print line,
        elif active_text:
            self.log.info(self.audit_format, "OK", entry["node"].name,
                          dest_path)

    def add_file(self, **kw):
        self.log.debug("add_file: %s", kw)
        self.files.append(kw)


class PlugIn:
    def __init__(self, manager, config, settings, node):
        self.log = logging.getLogger("plugin")
        self.manager = manager
        self.config = config
        self.settings = settings
        self.node = node

    def add_file(self, source_path, dest_path=None, source_text=None,
                 render=None, report=False, post_process=None, mode=None):
        render = render or self.render_cheetah
        return self.manager.add_file(node=self.node, config=self.config,
                                     dest_path=dest_path,
                                     source_path=source_path,
                                     source_text=source_text,
                                     render=render, report=report,
                                     post_process=post_process,
                                     mode=mode)

    def get_one(self, name, nodes=True, systems=False):
        hits = list(self.manager.confman.find(name, nodes=nodes,
                                              systems=systems))
        names = (h.name for h in hits)
        assert len(hits) == 1, "found more than one (%d) %r: %s" % (
            len(hits), name, ", ".join(names))

        return hits[0]

    def get_system(self, name):
        return self.get_one(name, nodes=False, systems=True)

    def add_edge(self, source, dest, **kwargs):
        self.manager.add_dynamic(dict(type="edge", source=source, dest=dest,
                                      **kwargs))

    def render_text(self, source_path, dest_path):
        try:
            return dest_path, file(source_path, "rb").read()
        except (IOError, OSError), error:
            raise errors.VerifyError(source_path, error)

    def render_cheetah(self, source_path, dest_path):
        try:
            names = dict(node=self.node, s=self.settings,
                         system=self.node.system,
                         find=self.manager.confman.find,
                         get_node=self.get_one,
                         get_system=self.get_system,
                         edge=self.add_edge,
                         dynconf=self.manager.dynamic_conf)
            text = str(CheetahTemplate(file=source_path, searchList=[names]))
            if dest_path:
                dest_path = str(CheetahTemplate(dest_path, searchList=[names]))

            return dest_path, text
        except Cheetah.Template.Error, error:
            raise errors.VerifyError(source_path, error)

