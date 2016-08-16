"""Distributed execution using an IPython cluster.

Uses IPython parallel to setup a cluster and manage execution:

http://ipython.org/ipython-doc/stable/parallel/index.html
Borrowed from Brad Chapman's implementation:
https://github.com/chapmanb/bcbio-nextgen/blob/master/bcbio/distributed/ipython.py
"""
import contextlib
import copy
import math
import os
import pipes
import uuid
import shutil
import subprocess
import time
from distutils.version import LooseVersion
import sys

from IPython.parallel import Client
from IPython.parallel.apps import launcher
from IPython.parallel import error as iperror
from IPython.utils.path import locate_profile
from IPython.utils import pickleutil
from IPython.utils import traitlets
from IPython.utils.traitlets import (List, Unicode, CRegExp)

from slurm import get_slurm_attributes
import utils
import lsf

# Dill not working for complex serialization
#try:
#    import dill
#except ImportError:
#    dill = None
dill = None

DEFAULT_MEM_PER_CPU = 1000  # Mb

# ## Custom launchers

# Handles longer timeouts for startup and shutdown
# with pings between engine and controller.
# Update the ping period to 15 seconds instead of 3.
# Shutdown engines if they cannot be contacted for 3 minutes
# from controller.
# Makes engine pingback shutdown higher, since this is
# not consecutive misses.
timeout_params = ["--timeout=60", "--IPEngineApp.wait_for_url_file=960",
                  "--EngineFactory.max_heartbeat_misses=100"]
controller_params = ["--nodb", "--hwm=1", "--scheme=leastload",
                     "--HeartMonitor.max_heartmonitor_misses=120",
                     "--HeartMonitor.period=30000"]

# ## Work around issues with docker and VM network interfaces
# Can go away when we merge changes into upstream IPython

import json
import stat
import netifaces
from IPython.parallel.apps.ipcontrollerapp import IPControllerApp
from IPython.utils.data import uniq_stable

class VMFixIPControllerApp(IPControllerApp):
    def _get_public_ip(self):
        """Avoid picking up docker and VM network interfaces in IPython 2.0.

        Adjusts _load_ips_netifaces from IPython.utils.localinterfaces. Changes
        submitted upstream so we can remove this when incorporated into released IPython.

        Prioritizes a set of common interfaces to try and make better decisions
        when choosing from multiple choices.
        """
        standard_ips = []
        priority_ips = []
        vm_ifaces = set(["docker0", "virbr0", "lxcbr0"])  # VM/container interfaces we do not want
        priority_ifaces = ("eth",)  # Interfaces we prefer to get IPs from

        # list of iface names, 'lo0', 'eth0', etc.
        for iface in netifaces.interfaces():
            if iface not in vm_ifaces:
                # list of ipv4 addrinfo dicts
                ipv4s = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
                for entry in ipv4s:
                    addr = entry.get('addr')
                    if not addr:
                        continue
                    if not (iface.startswith('lo') or addr.startswith('127.')):
                        if iface.startswith(priority_ifaces):
                            priority_ips.append(addr)
                        else:
                            standard_ips.append(addr)
        public_ips = uniq_stable(standard_ips + priority_ips)
        return public_ips[-1]

    def save_connection_dict(self, fname, cdict):
        """Override default save_connection_dict to use fixed public IP retrieval.
        """
        if not cdict.get("location"):
            cdict['location'] = self._get_public_ip()
            cdict["patch"] = "VMFixIPControllerApp"
        fname = os.path.join(self.profile_dir.security_dir, fname)
        self.log.info("writing connection info to %s", fname)
        with open(fname, 'w') as f:
            f.write(json.dumps(cdict, indent=2))
        os.chmod(fname, stat.S_IRUSR | stat.S_IWUSR)

# Increase resource limits on engines to handle additional processes
# At scale we can run out of open file handles or run out of user
# processes. This tries to adjust this limits for each IPython worker
# within available hard limits.
# Match target_procs to OSX limits for a default.
target_procs = 10240
resource_cmds = ["import resource",
                 "cur_proc, max_proc = resource.getrlimit(resource.RLIMIT_NPROC)",
                 "target_proc = min(max_proc, %s) if max_proc > 0 else %s" % (target_procs, target_procs),
                 "resource.setrlimit(resource.RLIMIT_NPROC, (max(cur_proc, target_proc), max_proc))",
                 "cur_hdls, max_hdls = resource.getrlimit(resource.RLIMIT_NOFILE)",
                 "target_hdls = min(max_hdls, %s) if max_hdls > 0 else %s" % (target_procs, target_procs),
                 "resource.setrlimit(resource.RLIMIT_NOFILE, (max(cur_hdls, target_hdls), max_hdls))"]

start_cmd = "from IPython.parallel.apps.%s import launch_new_instance"
engine_cmd_argv = [sys.executable, "-E", "-c"] + \
                  ["; ".join(resource_cmds + [start_cmd % "ipengineapp", "launch_new_instance()"])]
cluster_cmd_argv = [sys.executable, "-E", "-c"] + \
                   ["; ".join(resource_cmds + [start_cmd % "ipclusterapp", "launch_new_instance()"])]
#controller_cmd_argv = [sys.executable, "-E", "-c"] + \
#                      ["; ".join(resource_cmds + [start_cmd % "ipcontrollerapp", "launch_new_instance()"])]
controller_cmd_argv = [sys.executable, "-E", "-c"] + \
                      ["; ".join(resource_cmds + ["from cluster_helper.cluster import VMFixIPControllerApp",
                                                  "VMFixIPControllerApp.launch_instance()"])]

def get_engine_commands(context, n):
    """Retrieve potentially multiple engines running in a single submit script.

    Need to background the initial processes if multiple run.
    """
    assert n > 0
    engine_cmd_full = '%s %s --profile-dir="{profile_dir}" --cluster-id="{cluster_id}"' % \
                      (' '.join(map(pipes.quote, engine_cmd_argv)), ' '.join(timeout_params))
    out = [engine_cmd_full.format(**context)]
    for _ in range(n - 1):
        out.insert(0, "(%s &) &&" % (engine_cmd_full.format(**context)))
    return "\n".join(out)

# ## Platform LSF
class BcbioLSFEngineSetLauncher(launcher.LSFEngineSetLauncher):
    """Custom launcher handling heterogeneous clusters on LSF.
    """
    batch_file_name = Unicode(unicode("lsf_engine" + str(uuid.uuid4())))
    cores = traitlets.Integer(1, config=True)
    numengines = traitlets.Integer(1, config=True)
    mem = traitlets.Unicode("", config=True)
    tag = traitlets.Unicode("", config=True)
    resources = traitlets.Unicode("", config=True)
    job_array_template = Unicode('')
    queue_template = Unicode('')
    default_template = traitlets.Unicode("""#!/bin/sh
#BSUB -q {queue}
#BSUB -J {tag}-e[1-{n}]
#BSUB -oo bcbio-ipengine.bsub.%%J
#BSUB -n {cores}
#BSUB -R "span[hosts=1]"
{mem}
{resources}
{cmd}
""")

    def start(self, n):
        self.context["cores"] = self.cores * self.numengines
        if self.mem:
            # lsf.conf can specify nonstandard units for memory reservation
            lsf_unit = lsf.get_lsf_units(resource=True)
            mem = utils.convert_mb(float(self.mem) * 1024, lsf_unit)
            # check if memory reservation is per core or per job
            if lsf.per_core_reservation():
                mem = mem / self.cores
            mem = mem * self.numengines
            self.context["mem"] = '#BSUB -R "rusage[mem=%s]"' % mem
        else:
            self.context["mem"] = ""
        self.context["tag"] = self.tag if self.tag else "bcbio"
        self.context["resources"] = _format_lsf_resources(self.resources)
        self.context["cmd"] = get_engine_commands(self.context, self.numengines)
        return super(BcbioLSFEngineSetLauncher, self).start(n)

def _format_lsf_resources(resources):
    resource_str = ""
    for r in str(resources).split(";"):
        if r.strip():
            if "=" in r:
                arg, val = r.split("=")
                arg.strip()
                val.strip()
            else:
                arg = r.strip()
                val = ""
            resource_str += "#BSUB -%s %s\n" % (arg, val)
    return resource_str

class BcbioLSFControllerLauncher(launcher.LSFControllerLauncher):
    batch_file_name = Unicode(unicode("lsf_controller" + str(uuid.uuid4())))
    tag = traitlets.Unicode("", config=True)
    cores = traitlets.Integer(1, config=True)
    resources = traitlets.Unicode("", config=True)
    job_array_template = Unicode('')
    queue_template = Unicode('')
    default_template = traitlets.Unicode("""#!/bin/sh
#BSUB -q {queue}
#BSUB -J {tag}-c
#BSUB -n {cores}
#BSUB -oo bcbio-ipcontroller.bsub.%%J
{resources}
%s --ip=* --log-to-file --profile-dir="{profile_dir}" --cluster-id="{cluster_id}" %s
    """ % (' '.join(map(pipes.quote, controller_cmd_argv)),
           ' '.join(controller_params)))
    def start(self):
        self.context["cores"] = self.cores
        self.context["tag"] = self.tag if self.tag else "bcbio"
        self.context["resources"] = _format_lsf_resources(self.resources)
        return super(BcbioLSFControllerLauncher, self).start()

# ## Sun Grid Engine (SGE)

def _local_environment_exports():
    """Create export string for a batch script with environmental variables to pass.

    Passes additional environmental variables not inherited by some schedulers.
    LD_LIBRARY_PATH filtered by '-V' on recent Grid Engine releases.
    """
    exports = []
    for envname in ["LD_LIBRARY_PATH"]:
        envval = os.environ.get(envname)
        if envval:
            exports.append("export %s=%s" % (envname, envval))
    return "\n".join(exports)

class BcbioSGEEngineSetLauncher(launcher.SGEEngineSetLauncher):
    """Custom launcher handling heterogeneous clusters on SGE.
    """
    batch_file_name = Unicode(unicode("sge_engine" + str(uuid.uuid4())))
    cores = traitlets.Integer(1, config=True)
    pename = traitlets.Unicode("", config=True)
    resources = traitlets.Unicode("", config=True)
    mem = traitlets.Unicode("", config=True)
    memtype = traitlets.Unicode("mem_free", config=True)
    tag = traitlets.Unicode("", config=True)
    queue_template = Unicode('')
    default_template = traitlets.Unicode("""#$ -V
#$ -cwd
#$ -w w
#$ -j y
#$ -S /bin/sh
#$ -N {tag}-e
#$ -t 1-{n}
#$ -pe {pename} {cores}
{queue}
{mem}
{resources}
{exports}
echo \($SGE_TASK_ID - 1\) \* 0.5 | bc | xargs sleep
%s %s --profile-dir="{profile_dir}" --cluster-id="{cluster_id}"
""" % (' '.join(map(pipes.quote, engine_cmd_argv)),
       ' '.join(timeout_params)))

    def start(self, n):
        self.context["cores"] = self.cores
        if self.mem:
            if self.memtype == "rss":
                self.context["mem"] = "#$ -l rss=%sM" % int(float(self.mem) * 1024 / self.cores)
            else:
                self.context["mem"] = "#$ -l mem_free=%sM" % int(float(self.mem) * 1024)
        else:
            self.context["mem"] = ""
        if self.queue:
            self.context["queue"] = "#$ -q %s" % self.queue
        else:
            self.context["queue"] = ""
        self.context["tag"] = self.tag if self.tag else "bcbio"
        self.context["pename"] = str(self.pename)
        self.context["resources"] = "\n".join([_prep_sge_resource(r)
                                               for r in str(self.resources).split(";")
                                               if r.strip()])
        self.context["exports"] = _local_environment_exports()
        return super(BcbioSGEEngineSetLauncher, self).start(n)

class BcbioSGEControllerLauncher(launcher.SGEControllerLauncher):
    batch_file_name = Unicode(unicode("sge_controller" + str(uuid.uuid4())))
    tag = traitlets.Unicode("", config=True)
    cores = traitlets.Integer(1, config=True)
    pename = traitlets.Unicode("", config=True)
    resources = traitlets.Unicode("", config=True)
    queue_template = Unicode('')
    default_template = traitlets.Unicode(u"""#$ -V
#$ -cwd
#$ -w w
#$ -S /bin/sh
#$ -N {tag}-c
{cores}
{queue}
{resources}
{exports}
%s --ip=* --log-to-file --profile-dir="{profile_dir}" --cluster-id="{cluster_id}" %s
""" % (' '.join(map(pipes.quote, controller_cmd_argv)),
       ' '.join(controller_params)))
    def start(self):
        self.context["cores"] = "#$ -pe %s %s" % (self.pename, self.cores) if self.cores > 1 else ""
        self.context["tag"] = self.tag if self.tag else "bcbio"
        self.context["resources"] = "\n".join([_prep_sge_resource(r)
                                               for r in str(self.resources).split(";")
                                               if r.strip()])
        if self.queue:
            self.context["queue"] = "#$ -q %s" % self.queue
        else:
            self.context["queue"] = ""
        self.context["exports"] = _local_environment_exports()
        return super(BcbioSGEControllerLauncher, self).start()

def _prep_sge_resource(resource):
    """Prepare SGE resource specifications from the command line handling special cases.
    """
    resource = resource.strip()
    k, v = resource.split("=")
    if k in set(["ar"]):
        return "#$ -%s %s" % (k, v)
    else:
        return "#$ -l %s" % resource

def _find_parallel_environment(queue):
    """Find an SGE/OGE parallel environment for running multicore jobs in specified queue.
    """
    base_queue = os.path.splitext(queue)[0]
    queue = base_queue + ".q"

    available_pes = []
    for name in subprocess.check_output(["qconf", "-spl"]).strip().split():
        if name:
            for line in subprocess.check_output(["qconf", "-sp", name]).split("\n"):
                if _has_parallel_environment(line):
                    if (_queue_can_access_pe(name, queue) or _queue_can_access_pe(name, base_queue)):
                        available_pes.append(name)
    if len(available_pes) == 0:
        raise ValueError("Could not find an SGE environment configured for parallel execution. "
                         "See %s for SGE setup instructions." %
                         "https://blogs.oracle.com/templedf/entry/configuring_a_new_parallel_environment")
    else:
        return _prioritize_pes(available_pes)

def _prioritize_pes(choices):
    """Prioritize and deprioritize paired environments based on names.

    We're looking for multiprocessing friendly environments, so prioritize ones with SMP
    in the name and deprioritize those with MPI.
    """
    # lower scores = better
    ranks = {"smp": -1, "mpi": 1}
    sort_choices = []
    for n in choices:
        # Identify if it fits in any special cases
        special_case = False
        for k, val in ranks.items():
            if n.lower().find(k) >= 0:
                sort_choices.append((val, n))
                special_case = True
                break
        if not special_case:  # otherwise, no priority/de-priority
            sort_choices.append((0, n))
    sort_choices.sort()
    return sort_choices[0][1]

def _parseSGEConf(data):
    """Handle SGE multiple line output nastiness.
    From: https://github.com/clovr/vappio/blob/master/vappio-twisted/vappio_tx/load/sge_queue.py
    """
    lines = data.split('\n')
    multiline = False
    ret = {}
    for line in lines:
        line = line.strip()
        if line:
            if not multiline:
                key, value = line.split(' ', 1)
                value = value.strip().rstrip('\\')
                ret[key] = value
            else:
                # Making use of the fact that the key was created
                # in the previous iteration and is stil lin scope
                ret[key] += line
            multiline = (line[-1] == '\\')
    return ret

def _queue_can_access_pe(pe_name, queue):
    """Check if a queue has access to a specific parallel environment, using qconf.
    """
    try:
        queue_config = _parseSGEConf(subprocess.check_output(["qconf", "-sq", queue]))
    except:
        return False
    for test_pe_name in queue_config["pe_list"].split():
        test_pe_name = test_pe_name.split(",")[0].strip()
        if test_pe_name == pe_name:
            return True
    return False

def _has_parallel_environment(line):
    if line.startswith("allocation_rule"):
        if line.find("$pe_slots") >= 0 or line.find("$fill_up") >= 0:
                return True
    return False

# ## SLURM
class SLURMLauncher(launcher.BatchSystemLauncher):
    """A BatchSystemLauncher subclass for SLURM
    """
    submit_command = List(['sbatch'], config=True,
                          help="The SLURM submit command ['sbatch']")
    # Send SIGKILL instead of term, otherwise the job is "CANCELLED", not
    # "FINISHED"
    delete_command = List(['scancel', '--signal=KILL'], config=True,
                          help="The SLURM delete command ['scancel']")
    job_id_regexp = CRegExp(r'\d+', config=True,
                            help="A regular expression used to get the job id from the output of 'sbatch'")

    batch_file = Unicode(u'', config=True,
                         help="The string that is the batch script template itself.")

    queue_regexp = CRegExp('#SBATCH\W+-p\W+\w')
    queue_template = Unicode('#SBATCH -p {queue}')


class BcbioSLURMEngineSetLauncher(SLURMLauncher, launcher.BatchClusterAppMixin):
    """Custom launcher handling heterogeneous clusters on SLURM
    """
    batch_file_name = Unicode(unicode("SLURM_engine" + str(uuid.uuid4())))
    machines = traitlets.Integer(0, config=True)
    cores = traitlets.Integer(1, config=True)
    numengines = traitlets.Integer(1, config=True)
    mem = traitlets.Unicode("", config=True)
    tag = traitlets.Unicode("", config=True)
    account = traitlets.Unicode("", config=True)
    timelimit = traitlets.Unicode("", config=True)
    resources = traitlets.Unicode("", config=True)
    default_template = traitlets.Unicode("""#!/bin/sh
#SBATCH -p {queue}
#SBATCH -J {tag}-e[1-{n}]
#SBATCH -o bcbio-ipengine.out.%%j
#SBATCH -e bcbio-ipengine.err.%%j
#SBATCH --cpus-per-task={cores}
#SBATCH --array=1-{n}
#SBATCH -A {account}
#SBATCH -t {timelimit}
{machines}
{mem}
{resources}
{cmd}
""")

    def start(self, n):
        self.context["cores"] = self.cores * self.numengines
        if self.mem:
            self.context["mem"] = "#SBATCH --mem=%s" % int(float(self.mem) * 1024.0 * self.numengines)
        else:
            self.context["mem"] = "#SBATCH --mem=%d" % int(DEFAULT_MEM_PER_CPU * self.cores * self.numengines)
        self.context["tag"] = self.tag if self.tag else "bcbio"
        self.context["machines"] = ("#SBATCH %s" % (self.machines) if int(self.machines) > 0 else "")
        self.context["account"] = self.account
        self.context["timelimit"] = self.timelimit
        self.context["resources"] = "\n".join(["#SBATCH --%s" % r.strip()
                                               for r in str(self.resources).split(";")
                                               if r.strip()])
        self.context["cmd"] = get_engine_commands(self.context, self.numengines)
        return super(BcbioSLURMEngineSetLauncher, self).start(n)

class BcbioSLURMControllerLauncher(SLURMLauncher, launcher.BatchClusterAppMixin):
    batch_file_name = Unicode(unicode("SLURM_controller" + str(uuid.uuid4())))
    account = traitlets.Unicode("", config=True)
    cores = traitlets.Integer(1, config=True)
    timelimit = traitlets.Unicode("", config=True)
    mem = traitlets.Unicode("", config=True)
    tag = traitlets.Unicode("", config=True)
    resources = traitlets.Unicode("", config=True)
    default_template = traitlets.Unicode("""#!/bin/sh
#SBATCH -J {tag}-c
#SBATCH -o bcbio-ipcontroller.out.%%j
#SBATCH -e bcbio-ipcontroller.err.%%j
#SBATCH -A {account}
#SBATCH -t {timelimit}
#SBATCH --cpus-per-task={cores}
{mem}
{resources}
%s --ip=* --log-to-file --profile-dir="{profile_dir}" --cluster-id="{cluster_id}" %s
""" % (' '.join(map(pipes.quote, controller_cmd_argv)),
       ' '.join(controller_params)))
    def start(self):
        self.context["account"] = self.account
        self.context["timelimit"] = self.timelimit
        self.context["cores"] = self.cores
        self.context["mem"] = "#SBATCH --mem=%d" % (12 * DEFAULT_MEM_PER_CPU)
        self.context["tag"] = self.tag if self.tag else "bcbio"
        self.context["resources"] = "\n".join(["#SBATCH --%s" % r.strip()
                                               for r in str(self.resources).split(";")
                                               if r.strip()])
        return super(BcbioSLURMControllerLauncher, self).start(1)


class BcbioOLDSLURMEngineSetLauncher(SLURMLauncher, launcher.BatchClusterAppMixin):
    """Launch engines using SLURM for version < 2.6"""
    machines = traitlets.Integer(1, config=True)
    account = traitlets.Unicode("", config=True)
    timelimit = traitlets.Unicode("", config=True)
    batch_file_name = Unicode(unicode("SLURM_engines" + str(uuid.uuid4())),
                              config=True, help="batch file name for the engine(s) job.")

    default_template = Unicode(u"""#!/bin/sh
#SBATCH -A {account}
#SBATCH --job-name ipengine
#SBATCH -N {machines}
#SBATCH -t {timelimit}
srun -N {machines} -n {n} %s %s --profile-dir="{profile_dir}" --cluster-id="{cluster_id}"
    """ % (' '.join(map(pipes.quote, engine_cmd_argv)),
           ' '.join(timeout_params)))

    def start(self, n):
        """Start n engines by profile or profile_dir."""
        self.context["machines"] = self.machines
        self.context["account"] = self.account
        self.context["timelimit"] = self.timelimit
        return super(BcbioOLDSLURMEngineSetLauncher, self).start(n)


class BcbioOLDSLURMControllerLauncher(SLURMLauncher, launcher.BatchClusterAppMixin):
    """Launch a controller using SLURM for versions < 2.6"""
    account = traitlets.Unicode("", config=True)
    timelimit = traitlets.Unicode("", config=True)
    batch_file_name = Unicode(unicode("SLURM_controller" + str(uuid.uuid4())),
                              config=True, help="batch file name for the engine(s) job.")

    default_template = Unicode("""#!/bin/sh
#SBATCH -A {account}
#SBATCH --job-name ipcontroller
#SBATCH -t {timelimit}
%s --ip=* --log-to-file --profile-dir="{profile_dir}" --cluster-id="{cluster_id}" %s
""" % (' '.join(map(pipes.quote, controller_cmd_argv)),
       ' '.join(controller_params)))

    def start(self):
        """Start the controller by profile or profile_dir."""
        self.context["account"] = self.account
        self.context["timelimit"] = self.timelimit
        return super(BcbioOLDSLURMControllerLauncher, self).start(1)

# ## Torque
class TORQUELauncher(launcher.BatchSystemLauncher):
    """A BatchSystemLauncher subclass for Torque"""

    submit_command = List(['qsub'], config=True,
                          help="The PBS submit command ['qsub']")
    delete_command = List(['qdel'], config=True,
                          help="The PBS delete command ['qsub']")
    job_id_regexp = CRegExp(r'\d+(\[\])?', config=True,
                            help="Regular expresion for identifying the job ID [r'\d+']")

    batch_file = Unicode(u'')
    #job_array_regexp = CRegExp('#PBS\W+-t\W+[\w\d\-\$]+')
    #job_array_template = Unicode('#PBS -t 1-{n}')
    queue_regexp = CRegExp('#PBS\W+-q\W+\$?\w+')
    queue_template = Unicode('#PBS -q {queue}')

def _prep_torque_resources(resources):
    """Prepare resources passed to torque from input parameters.
    """
    out = []
    has_walltime = False
    for r in resources.split(";"):
        if "=" in r:
            k, v = r.split("=")
            k.strip()
            v.strip()
        else:
            k = ""
        if k.lower() in ["a", "account", "acct"] and v:
            out.append("#PBS -A %s" % v)
        elif r.strip():
            if k.lower() == "walltime":
                has_walltime = True
            out.append("#PBS -l %s" % r.strip())
    if not has_walltime:
        out.append("#PBS -l walltime=239:00:00")
    return out

class BcbioTORQUEEngineSetLauncher(TORQUELauncher, launcher.BatchClusterAppMixin):
    """Launch Engines using Torque"""
    cores = traitlets.Integer(1, config=True)
    mem = traitlets.Unicode("", config=True)
    tag = traitlets.Unicode("", config=True)
    resources = traitlets.Unicode("", config=True)
    batch_file_name = Unicode(unicode("torque_engines" + str(uuid.uuid4())),
                              config=True, help="batch file name for the engine(s) job.")
    default_template = Unicode(u"""#!/bin/sh
#PBS -V
#PBS -j oe
#PBS -N {tag}-e
#PBS -t 1-{n}
#PBS -l nodes=1:ppn={cores}
{mem}
{resources}
cd $PBS_O_WORKDIR
%s %s --profile-dir="{profile_dir}" --cluster-id="{cluster_id}"
    """ % (' '.join(map(pipes.quote, engine_cmd_argv)),
           ' '.join(timeout_params)))

    def start(self, n):
        """Start n engines by profile or profile_dir."""
        try:
            self.context["cores"] = self.cores
            if self.mem:
                self.context["mem"] = "#PBS -l mem=%smb" % int(float(self.mem) * 1024)
            else:
                self.context["mem"] = ""
            self.context["tag"] = self.tag if self.tag else "bcbio"
            self.context["resources"] = "\n".join(_prep_torque_resources(self.resources))
            return super(BcbioTORQUEEngineSetLauncher, self).start(n)
        except:
            self.log.exception("Engine start failed")

class BcbioTORQUEControllerLauncher(TORQUELauncher, launcher.BatchClusterAppMixin):
    """Launch a controller using Torque."""
    batch_file_name = Unicode(unicode("torque_controller" + str(uuid.uuid4())),
                              config=True, help="batch file name for the engine(s) job.")
    cores = traitlets.Integer(1, config=True)
    tag = traitlets.Unicode("", config=True)
    resources = traitlets.Unicode("", config=True)
    default_template = Unicode("""#!/bin/sh
#PBS -V
#PBS -N {tag}-c
#PBS -j oe
#PBS -l nodes=1:ppn={cores}
{resources}
cd $PBS_O_WORKDIR
%s --ip=* --log-to-file --profile-dir="{profile_dir}" --cluster-id="{cluster_id}" %s
""" % (' '.join(map(pipes.quote, controller_cmd_argv)),
       ' '.join(controller_params)))

    def start(self):
        """Start the controller by profile or profile_dir."""
        try:
            self.context["cores"] = self.cores
            self.context["tag"] = self.tag if self.tag else "bcbio"
            self.context["resources"] = "\n".join(_prep_torque_resources(self.resources))
            return super(BcbioTORQUEControllerLauncher, self).start(1)
        except:
            self.log.exception("Controller start failed")

# ## PBSPro
class PBSPROLauncher(launcher.PBSLauncher):
    """A BatchSystemLauncher subclass for PBSPro."""
    job_array_regexp = CRegExp('#PBS\W+-J\W+[\w\d\-\$]+')
    job_array_template = Unicode('')

class BcbioPBSPROEngineSetLauncher(PBSPROLauncher, launcher.BatchClusterAppMixin):
    """Launch Engines using PBSPro"""
    batch_file_name = Unicode(u'pbspro_engines', config=True,
                              help="batch file name for the engine(s) job.")
    tag = traitlets.Unicode("", config=True)
    cores = traitlets.Integer(1, config=True)
    mem = traitlets.Unicode("", config=True)
    array_cmd = traitlets.Unicode("", config=True)
    default_template = Unicode(u"""#!/bin/sh
#PBS -V
#PBS -N {tag}-e
{resources}
{array_cmd}
cd $PBS_O_WORKDIR
%s %s --profile-dir="{profile_dir}" --cluster-id="{cluster_id}"
    """ % (' '.join(map(pipes.quote, engine_cmd_argv)),
           ' '.join(timeout_params)))

    def start(self, n):
        """Start n engines by profile or profile_dir."""
        resources = "#PBS -l select=1:ncpus=%d" % self.cores
        if self.mem:
            resources += ":mem=%smb" % int(float(self.mem) * 1024)
        self.context["resources"] = resources
        self.context["cores"] = self.cores
        self.context["tag"] = self.tag if self.tag else "bcbio"
        self.context["array_cmd"] = "" if n == 1 else "#PBS -J 1-%i" % n
        return super(BcbioPBSPROEngineSetLauncher, self).start(n)


class BcbioPBSPROControllerLauncher(PBSPROLauncher, launcher.BatchClusterAppMixin):
    """Launch a controller using PBSPro."""

    batch_file_name = Unicode(u'pbspro_controller', config=True,
                              help="batch file name for the controller job.")
    tag = traitlets.Unicode("", config=True)
    cores = traitlets.Integer(1, config=True)
    default_template = Unicode("""#!/bin/sh
#PBS -V
#PBS -N {tag}-c
#PBS -l select=1:ncpus={cores}
cd $PBS_O_WORKDIR
%s --ip=* --log-to-file --profile-dir="{profile_dir}" --cluster-id="{cluster_id}" %s
""" % (' '.join(map(pipes.quote, controller_cmd_argv)),
       ' '.join(controller_params)))

    def start(self):
        """Start the controller by profile or profile_dir."""
        self.context["cores"] = self.cores
        self.context["tag"] = self.tag if self.tag else "bcbio"
        return super(BcbioPBSPROControllerLauncher, self).start(1)


def _get_profile_args(profile):
    if os.path.isdir(profile) and os.path.isabs(profile):
        return ["--profile-dir=%s" % profile]
    else:
        return ["--profile=%s" % profile]

def _scheduler_resources(scheduler, params, queue):
    """Retrieve custom resource tweaks for specific schedulers.
    Handles SGE parallel environments, which allow multicore jobs
    but are specific to different environments.
    Pulls out hacks to work in different environments:
      - mincores -- Require a minimum number of cores when submitting jobs
                    to avoid single core jobs on constrained queues
    """
    orig_resources = copy.deepcopy(params.get("resources", []))
    specials = {}
    if not orig_resources:
        orig_resources = []
    if isinstance(orig_resources, basestring):
        orig_resources = orig_resources.split(";")
    resources = []
    for r in orig_resources:
        if r.startswith("mincores="):
            specials["mincores"] = int(r.split("=")[-1])
        else:
            resources.append(r)
    if scheduler in ["SGE"]:
        pass_resources = []
        for r in resources:
            if r.startswith("pename="):
                _, pename = r.split("=")
                specials["pename"] = pename
            elif r.startswith("memtype="):
                _, memtype = r.split("=")
                specials["memtype"] = memtype
            else:
                pass_resources.append(r)
        resources = pass_resources
        if "pename" not in specials:
            specials["pename"] = _find_parallel_environment(queue)
    return ";".join(resources), specials

def _start(scheduler, profile, queue, num_jobs, cores_per_job, cluster_id,
           extra_params):
    """Starts cluster from commandline.
    """
    ns = "cluster_helper.cluster"
    scheduler = scheduler.upper()
    if scheduler == "SLURM" and _slurm_is_old():
        scheduler = "OLDSLURM"
    engine_class = "Bcbio%sEngineSetLauncher" % scheduler
    controller_class = "Bcbio%sControllerLauncher" % scheduler
    if not (engine_class in globals() and controller_class in globals()):
        print ("The engine and controller class %s and %s are not "
               "defined. " % (engine_class, controller_class))
        print ("This may be due to ipython-cluster-helper not supporting "
               "your scheduler. If it should, please file a bug report at "
               "http://github.com/roryk/ipython-cluster-helper. Thanks!")
        sys.exit(1)
    resources, specials = _scheduler_resources(scheduler, extra_params, queue)
    if scheduler in ["OLDSLURM", "SLURM"]:
        resources, slurm_atrs = get_slurm_attributes(queue, resources)
    else:
        slurm_atrs = None
    mincores = specials.get("mincores", 1)
    if mincores > cores_per_job:
        mincores = int(math.ceil(mincores / float(cores_per_job)))
        num_jobs = int(math.ceil(num_jobs / float(mincores)))

    args = cluster_cmd_argv + \
        ["start",
         "--daemonize=True",
         "--IPClusterEngines.early_shutdown=240",
         "--delay=10",
         "--log-to-file",
         "--debug",
         "--n=%s" % num_jobs,
         "--%s.cores=%s" % (controller_class, min(mincores, 2)),
         "--%s.cores=%s" % (engine_class, cores_per_job),
         "--%s.resources='%s'" % (controller_class, resources),
         "--%s.resources='%s'" % (engine_class, resources),
         "--%s.mem='%s'" % (engine_class, extra_params.get("mem", "")),
         "--%s.mem='%s'" % (controller_class, extra_params.get("mem", "")),
         "--%s.tag='%s'" % (engine_class, extra_params.get("tag", "")),
         "--%s.tag='%s'" % (controller_class, extra_params.get("tag", "")),
         "--IPClusterStart.controller_launcher_class=%s.%s" % (ns, controller_class),
         "--IPClusterStart.engine_launcher_class=%s.%s" % (ns, engine_class),
         "--%sLauncher.queue='%s'" % (scheduler, queue),
         "--cluster-id=%s" % (cluster_id)
         ]
    args += _get_profile_args(profile)
    if mincores > 1 and mincores > cores_per_job:
        args += ["--%s.numengines=%s" % (engine_class, mincores)]
    if specials.get("pename"):
        args += ["--%s.pename=%s" % (controller_class, specials["pename"])]
        args += ["--%s.pename=%s" % (engine_class, specials["pename"])]
    if specials.get("memtype"):
        args += ["--%s.memtype=%s" % (engine_class, specials["memtype"])]
    if slurm_atrs:
        args += ["--%s.machines=%s" % (engine_class, slurm_atrs.get("machines", "0"))]
        args += ["--%s.account=%s" % (engine_class, slurm_atrs["account"])]
        args += ["--%s.account=%s" % (controller_class, slurm_atrs["account"])]
        args += ["--%s.timelimit='%s'" % (engine_class, slurm_atrs["timelimit"])]
        args += ["--%s.timelimit='%s'" % (controller_class, slurm_atrs["timelimit"])]
    subprocess.check_call(args)
    return cluster_id

def _start_local(cores, profile, cluster_id):
    """Start a local non-distributed IPython engine. Useful for testing
    """
    args = cluster_cmd_argv + \
        ["start",
         "--daemonize=True",
         "--log-to-file",
         "--debug",
         "--cluster-id=%s" % cluster_id,
         "--n=%s" % cores]
    args += _get_profile_args(profile)
    subprocess.check_call(args)
    return cluster_id

def stop_from_view(view):
    _stop(view.clusterhelper["profile"], view.clusterhelper["cluster_id"])

def _stop(profile, cluster_id):
    args = cluster_cmd_argv + \
           ["stop", "--cluster-id=%s" % cluster_id]
    args += _get_profile_args(profile)
    subprocess.check_call(args)

@contextlib.contextmanager
def cluster_view(scheduler, queue, num_jobs, cores_per_job=1, profile=None,
                 start_wait=16, extra_params=None, retries=None, direct=False):
    """Provide a view on an ipython cluster for processing.

      - scheduler: The type of cluster to start (lsf, sge, pbs, torque).
      - num_jobs: Number of jobs to start.
      - cores_per_job: The number of cores to use for each job.
      - start_wait: How long to wait for the cluster to startup, in minutes.
        Defaults to 16 minutes. Set to longer for slow starting clusters.
      - retries: Number of retries to allow for failed tasks.
    """
    num_jobs = int(num_jobs)
    cores_per_job = int(cores_per_job)
    start_wait = int(start_wait)

    if extra_params is None:
        extra_params = {}
    max_delay = start_wait * 60
    delay = 5 if extra_params.get("run_local") else 30
    max_tries = 10
    if profile is None:
        has_throwaway = True
        profile = create_throwaway_profile()
    else:
        # ensure we have an .ipython directory to prevent issues
        # creating it during parallel startup
        cmd = [sys.executable, "-E", "-c", "from IPython import start_ipython; start_ipython()",
               "profile", "create", "--parallel"] + _get_profile_args(profile)
        subprocess.check_call(cmd)
        has_throwaway = False
    num_tries = 0

    cluster_id = str(uuid.uuid4())
    url_file = get_url_file(profile, cluster_id)

    while 1:
        try:
            if extra_params.get("run_local"):
                _start_local(num_jobs, profile, cluster_id)
            else:
                _start(scheduler, profile, queue, num_jobs, cores_per_job, cluster_id, extra_params)
            break
        except subprocess.CalledProcessError:
            if num_tries > max_tries:
                raise
            num_tries += 1
            time.sleep(delay)
    try:
        need_engines = 1  # Start using cluster when this many engines are up
        client = None
        slept = 0
        max_up = 0
        up = 0
        while up < need_engines:
            up = _nengines_up(url_file)
            if up < max_up:
                print ("Engine(s) that were up have shutdown prematurely. "
                       "Aborting cluster startup.")
                _stop(profile, cluster_id)
                sys.exit(1)
            max_up = up
            time.sleep(delay)
            slept += delay
            if slept > max_delay:
                raise IOError("Cluster startup timed out.")
        client = Client(url_file, timeout=60)
        if direct:
            view = _get_direct_view(client, retries)
        else:
            view = _get_balanced_blocked_view(client, retries)
        view.clusterhelper = {"profile": profile, "cluster_id": cluster_id}
        if dill:
            pickleutil.use_dill()
            view.apply(pickleutil.use_dill)
        yield view
    finally:
        if client:
            _shutdown(client)
        _stop(profile, cluster_id)
        if has_throwaway:
            delete_profile(profile)

def _nengines_up(url_file):
    "return the number of engines up"
    client = None
    try:
        client = Client(url_file, timeout=60)
        up = len(client.ids)
        client.close()
    # the controller isn't up yet
    except iperror.TimeoutError:
        return 0
    # the JSON file is not available to parse
    except IOError:
        return 0
    else:
        return up

def _get_balanced_blocked_view(client, retries):
    view = client.load_balanced_view()
    view.set_flags(block=True)
    if retries:
        view.set_flags(retries=int(retries))
    return view

def _shutdown(client):
    print "Sending a shutdown signal to the controller and engines."
    client.close()

def _get_direct_view(client, retries):
    view = client[:]
    view.set_flags(block=True)
    if retries:
        view.set_flags(retries=int(retries))
    return view

def _slurm_is_old():
    return LooseVersion(_slurm_version()) < LooseVersion("2.6")

def _slurm_version():
    version_line = subprocess.Popen("sinfo -V", shell=True,
                                    stdout=subprocess.PIPE).communicate()[0]
    parts = version_line.split()
    if len(parts) > 0:
        return version_line.split()[1]
    else:
        return "2.6+"

# ## Temporary profile management

def create_throwaway_profile():
    profile = str(uuid.uuid1())
    cmd = [sys.executable, "-E", "-c", "from IPython import start_ipython; start_ipython()",
           "profile", "create", profile, "--parallel"]
    subprocess.check_call(cmd)
    return profile

def get_url_file(profile, cluster_id):

    url_file = "ipcontroller-{0}-client.json".format(cluster_id)

    if os.path.isdir(profile) and os.path.isabs(profile):
        # Return full_path if one is given
        return os.path.join(profile, "security", url_file)

    return os.path.join(locate_profile(profile), "security", url_file)

def delete_profile(profile):
    MAX_TRIES = 10
    dir_to_remove = locate_profile(profile)
    if os.path.exists(dir_to_remove):
        num_tries = 0
        while True:
            try:
                shutil.rmtree(dir_to_remove)
                break
            except OSError:
                if num_tries > MAX_TRIES:
                    raise
                time.sleep(5)
                num_tries += 1
    else:
        raise ValueError("Cannot find {0} to remove, "
                         "something is wrong.".format(dir_to_remove))
