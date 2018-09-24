# Copyright 2018 Datawire. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import ssl
import sys
import os

import json
from subprocess import CalledProcessError, STDOUT
from shutil import which
from typing import List
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from telepresence.runner import Runner

# IP that shouldn't be in use on Internet, *or* local networks:
MAC_LOOPBACK_IP = "198.18.0.254"


def kubectl_or_oc(server: str) -> str:
    """
    Return "kubectl" or "oc", the command-line tool we should use.

    :param server: The URL of the cluster API server.
    """
    if which("oc") is None:
        return "kubectl"
    # We've got oc, and possibly kubectl as well. We only want oc for OpenShift
    # servers, so check for an OpenShift API endpoint:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urlopen(server + "/version/openshift", context=ctx) as u:
            u.read()
    except (URLError, HTTPError):
        return "kubectl"
    else:
        return "oc"


class KubeInfo(object):
    """Record the local machine Kubernetes configuration"""

    def __init__(self, runner: Runner, args) -> None:
        span = runner.span()
        # We don't quite know yet if we want kubectl or oc (if someone has both
        # it depends on the context), so until we know the context just guess.
        # We prefer kubectl over oc insofar as (1) kubectl commands we do in
        # this prelim setup stage don't require oc and (2) sometimes oc is a
        # different binary unrelated to OpenShift.
        missing = runner.depend(["kubectl", "oc"])
        if "kubectl" not in missing:
            prelim_command = "kubectl"
        elif "oc" not in missing:
            prelim_command = "oc"
        else:
            raise runner.fail(
                "Found neither 'kubectl' nor 'oc' in your $PATH."
            )

        try:
            kubectl_version_output = runner.get_output([
                prelim_command, "version", "--short"
            ]).split("\n")
            self.kubectl_version = kubectl_version_output[0].split(": v")[1]
            self.cluster_version = kubectl_version_output[1].split(": v")[1]
        except CalledProcessError as exc:
            ver = "(error: {})".format(exc)
            self.kubectl_version = self.cluster_version = ver

        # Make sure we have a Kubernetes context set either on command line or
        # in kubeconfig:
        if args.context is None:
            try:
                args.context = runner.get_output(
                    [prelim_command, "config", "current-context"],
                    stderr=STDOUT,
                )
            except CalledProcessError:
                sudo_used = ""
                if os.geteuid() == 0:
                    sudo_used = "Sudo user detected. " + \
                        "We can't find a context " + \
                        "and maybe that's because we're running as root. " + \
                        "Try running without sudo."

                raise runner.fail(
                    "No current-context set. "
                    "Please use the --context option to explicitly set the "
                    "context."
                    "\n{}".format(sudo_used)
                )
        self.context = args.context

        # Figure out explicit namespace if its not specified, and the server
        # address (we use the server address to determine for good whether we
        # want oc or kubectl):
        kubectl_config = json.loads(
            runner.get_output([prelim_command, "config", "view", "-o", "json"])
        )
        for context_setting in kubectl_config["contexts"]:
            if context_setting["name"] == args.context:
                if args.namespace is None:
                    args.namespace = context_setting["context"].get(
                        "namespace", "default"
                    )
                self.cluster = context_setting["context"]["cluster"]
                break
        else:
            raise runner.fail("Error: Unable to find cluster information")

        # Check if the requested namespace exists
        try:
            runner.get_output([prelim_command, "--context", self.context,
                               "get", "ns", args.namespace]).split("\n")
            self.namespace = args.namespace
        except CalledProcessError:
            raise runner.fail(
                "Error: Namespace '{}' does not exist".format(args.namespace)
            )

        for cluster_setting in kubectl_config["clusters"]:
            if cluster_setting["name"] == self.cluster:
                self.server = cluster_setting["cluster"]["server"]
                break
        else:
            raise runner.fail("Error: Unable to find server information")

        self.command = kubectl_or_oc(self.server)
        self.verbose = args.verbose

        runner.write(
            "Command: {} {}".format(self.command, self.kubectl_version)
        )
        runner.write(
            "Context: {}, namespace: {}, version: {}\n".format(
                self.context, self.namespace, self.cluster_version
            )
        )
        self.in_local_vm = self._check_if_in_local_vm(runner)
        if self.in_local_vm:
            runner.write("Looks like we're in a local VM, e.g. minikube.\n")

        span.end()

    def __call__(self, *in_args) -> List[str]:
        """Return command-line for running kubectl."""
        # Allow kubectl(arg1, arg2, arg3) or kubectl(*args) but also allow
        # kubectl(args) for convenience.
        if len(in_args) == 1 and type(in_args[0]) is not str:
            args = in_args[0]
        else:
            args = in_args
        result = [self.command]
        if self.verbose:
            result.append("--v=4")
        result.extend(["--context", self.context])
        result.extend(["--namespace", self.namespace])
        result += args
        return result

    def _check_if_in_local_vm(self, runner: Runner) -> bool:
        # Minikube just has 'minikube' as context'
        if self.context == "minikube":
            return True
        # Minishift has complex context name, so check by server:
        if self.command == "oc":
            try:
                ip = runner.get_output(["minishift", "ip"]).strip()
            except (OSError, CalledProcessError):
                return False
            if ip and ip in runner.kubectl.server:
                return True
        return False


def final_checks(runner: Runner, args):
    """
    Perform some last cross-cutting checks
    """

    # Make sure we can access Kubernetes:
    try:
        runner.get_output(
            runner.kubectl(
                "get", "pods", "telepresence-connectivity-check",
                "--ignore-not-found"
            ),
            stderr=STDOUT,
        )
    except CalledProcessError as exc:
        sys.stderr.write("Error accessing Kubernetes: {}\n".format(exc))
        if exc.output:
            sys.stderr.write("{}\n".format(exc.output.strip()))
        raise runner.fail("Cluster access failed")
    except (OSError, IOError) as exc:
        raise runner.fail(
            "Unexpected error accessing Kubernetes: {}\n".format(exc)
        )
