import os
import time
import json
import select
import atexit
import shlex
import time
import re

from metaflow import util
from metaflow.datastore.util.s3tail import S3Tail
from metaflow.exception import MetaflowException, MetaflowInternalError
from metaflow.metaflow_config import (
    BATCH_METADATA_SERVICE_URL,
    DATATOOLS_S3ROOT,
    DATASTORE_LOCAL_DIR,
    DATASTORE_SYSROOT_S3,
    DEFAULT_METADATA,
    BATCH_METADATA_SERVICE_HEADERS,
)
from metaflow.mflog import (
    export_mflog_env_vars,
    bash_capture_logs,
    update_delay,
    BASH_SAVE_LOGS,
    TASK_LOG_SOURCE,
)
from metaflow.mflog.mflog import refine, set_should_persist

from .kubernetes_client import KubernetesClient

# Redirect structured logs to /logs/
LOGS_DIR = "/logs"
STDOUT_FILE = "mflog_stdout"
STDERR_FILE = "mflog_stderr"
STDOUT_PATH = os.path.join(LOGS_DIR, STDOUT_FILE)
STDERR_PATH = os.path.join(LOGS_DIR, STDERR_FILE)


class KubernetesException(MetaflowException):
    headline = "Kubernetes error"


class KubernetesKilledException(MetaflowException):
    headline = "Kubernetes Batch job killed"


class Kubernetes(object):
    def __init__(
        self,
        datastore,
        metadata,
        environment,
        flow_name,
        run_id,
        step_name,
        task_id,
        attempt,
    ):
        self._datastore = datastore
        self._metadata = metadata
        self._environment = environment

        self._flow_name = flow_name
        self._run_id = run_id
        self._step_name = step_name
        self._task_id = task_id
        self._attempt = str(attempt)

        # TODO: Issue a kill request for all pending Kubernetes jobs at exit.
        # atexit.register(
        #     lambda: self.job.kill() if hasattr(self, 'job') else None)

    def _command(
        self,
        code_package_url,
        step_cmds,
    ):
        mflog_expr = export_mflog_env_vars(
            flow_name=self._flow_name,
            run_id=self._run_id,
            step_name=self._step_name,
            task_id=self._task_id,
            retry_count=self._attempt,
            datastore_type=self._datastore.TYPE,
            stdout_path=STDOUT_PATH,
            stderr_path=STDERR_PATH,
        )
        init_cmds = self._environment.get_package_commands(code_package_url)
        init_expr = " && ".join(init_cmds)
        step_expr = bash_capture_logs(
            " && ".join(
                self._environment.bootstrap_commands(self._step_name)
                + step_cmds
            )
        )

        # Construct an entry point that
        # 1) initializes the mflog environment (mflog_expr)
        # 2) bootstraps a metaflow environment (init_expr)
        # 3) executes a task (step_expr)

        # The `true` command is to make sure that the generated command
        # plays well with docker containers which have entrypoint set as
        # eval $@
        cmd_str = "true && mkdir -p /logs && %s && %s && %s; " % (
            mflog_expr,
            init_expr,
            step_expr,
        )
        # After the task has finished, we save its exit code (fail/success)
        # and persist the final logs. The whole entrypoint should exit
        # with the exit code (c) of the task.
        #
        # Note that if step_expr OOMs, this tail expression is never executed.
        # We lose the last logs in this scenario.
        #
        # TODO: Find a way to capture hard exit logs in Kubernetes.
        cmd_str += "c=$?; %s; exit $c" % BASH_SAVE_LOGS
        return shlex.split('bash -c "%s"' % cmd_str)

    def launch_job(self, **kwargs):
        self._job = self.create_job(**kwargs).execute()

    def create_job(
        self,
        user,
        code_package_sha,
        code_package_url,
        code_package_ds,
        step_cli,
        docker_image,
        namespace=None,
        service_account=None,
        cpu=None,
        gpu=None,
        memory=None,
        run_time_limit=None,
        env={},
    ):
        # TODO: Test for DNS-1123 compliance. Python names can have underscores
        #       which are not valid Kubernetes names. We can potentially make
        #       the pathspec DNS-1123 compliant by stripping away underscores
        #       etc. and relying on Kubernetes to attach a suffix to make the
        #       name unique within a namespace.
        #
        # Set the pathspec (along with attempt) as the Kubernetes job name.
        # Kubernetes job names are supposed to be unique within a Kubernetes
        # namespace and compliant with DNS-1123. The pathspec (with attempt)
        # can provide that guarantee, however, for flows launched via AWS Step
        # Functions (and potentially Argo), we may not get the task_id or the
        # attempt_id while submitting the job to the Kubernetes cluster. If
        # that is indeed the case, we can rely on Kubernetes to generate a name
        # for us.
        job_name = "-".join(
            [
                self._flow_name,
                self._run_id,
                self._step_name,
                self._task_id,
                self._attempt,
            ]
        ).lower()

        job = (
            KubernetesClient()
            .job(
                name=job_name,
                namespace=namespace,
                service_account=service_account,
                command=self._command(
                    code_package_url=code_package_url,
                    step_cmds=[step_cli],
                ),
                image=docker_image,
                cpu=cpu,
                memory=memory,
                timeout_in_seconds=run_time_limit,
                # Retries are handled by Metaflow runtime
                retries=0,
            )
            .environment_variable(
                # This is needed since `boto3` is not smart enough to figure out
                # AWS region by itself.
                "AWS_DEFAULT_REGION",
                "us-west-2",
            )
            .environment_variable("METAFLOW_CODE_SHA", code_package_sha)
            .environment_variable("METAFLOW_CODE_URL", code_package_url)
            .environment_variable("METAFLOW_CODE_DS", code_package_ds)
            .environment_variable("METAFLOW_USER", user)
            .environment_variable(
                "METAFLOW_SERVICE_URL", BATCH_METADATA_SERVICE_URL
            )
            .environment_variable(
                "METAFLOW_SERVICE_HEADERS",
                json.dumps(BATCH_METADATA_SERVICE_HEADERS),
            )
            .environment_variable(
                "METAFLOW_DATASTORE_SYSROOT_S3", DATASTORE_SYSROOT_S3
            )
            .environment_variable("METAFLOW_DATATOOLS_S3ROOT", DATATOOLS_S3ROOT)
            .environment_variable("METAFLOW_DEFAULT_DATASTORE", "s3")
            .environment_variable("METAFLOW_DEFAULT_METADATA", DEFAULT_METADATA)
            .environment_variable("METAFLOW_KUBERNETES_WORKLOAD", 1)
            .label("app", "metaflow")
            .label("metaflow/flow_name", self._flow_name)
            .label("metaflow/run_id", self._run_id)
            .label("metaflow/step_name", self._step_name)
            .label("metaflow/task_id", self._task_id)
            .label("metaflow/attempt", self._attempt)
        )

        # Skip setting METAFLOW_DATASTORE_SYSROOT_LOCAL because metadata sync
        # between the local user instance and the remote Kubernetes pod
        # assumes metadata is stored in DATASTORE_LOCAL_DIR on the Kubernetes
        # pod; this happens when METAFLOW_DATASTORE_SYSROOT_LOCAL is NOT set (
        # see get_datastore_root_from_config in datastore/local.py).
        for name, value in env.items():
            job.environment_variable(name, value)

        # Add labels to the Kubernetes job
        #
        # Apply recommended labels https://kubernetes.io/docs/concepts/overview/working-with-objects/common-labels/
        #
        # TODO: 1. Verify the behavior of high cardinality labels like instance,
        #          version etc. in the app.kubernetes.io namespace before 
        #          introducing them here.
        job.label("app.kubernetes.io/name", "metaflow-task").label(
            "app.kubernetes.io/part-of", "metaflow"
        ).label("app.kubernetes.io/created-by", user)
        # Add Metaflow system tags as labels as well!
        # 
        # TODO  1. Label values must be an empty string or consist of 
        #          alphanumeric characters, '-', '_' or '.', and must start and 
        #          end with an alphanumeric character. Fix the simple regex
        #          match below.
        for sys_tag in self._metadata.sticky_sys_tags:
            job.label(
                "metaflow/%s" % sys_tag[: sys_tag.index(":")],
                re.sub("[^A-Za-z0-9.-_]", ".", sys_tag[sys_tag.index(":") + 1 :]),
            )
        # TODO: Add annotations based on https://kubernetes.io/blog/2021/04/20/annotating-k8s-for-humans/

        return job.create()

    def wait(self, echo=None):
        ds = self._datastore(
            mode="w",
            flow_name=self._flow_name,
            run_id=self._run_id,
            step_name=self._step_name,
            task_id=self._task_id,
            attempt=int(self._attempt),
        )
        stdout_location = ds.get_log_location(TASK_LOG_SOURCE, "stdout")
        stderr_location = ds.get_log_location(TASK_LOG_SOURCE, "stderr")

        def wait_for_launch(job):
            status = job.status
            echo(
                "Task is starting (status %s)..." % status,
                "stderr",
                job_id=job.id,
            )
            t = time.time()
            while True:
                if status != job.status or (time.time() - t) > 30:
                    status = job.status
                    echo(
                        "Task is starting (status %s)..." % status,
                        "stderr",
                        job_id=job.id,
                    )
                    t = time.time()
                if job.is_running or job.is_done:
                    break
                select.poll().poll(200)

        def _print_available(tail, stream, should_persist=False):
            # print the latest batch of lines from S3Tail
            prefix = b"[%s] " % util.to_bytes(self._job.id)
            try:
                for line in tail:
                    if should_persist:
                        line = set_should_persist(line)
                    else:
                        line = refine(line, prefix=prefix)
                    echo(line.strip().decode("utf-8", errors="replace"), stream)
            except Exception as ex:
                echo(
                    "[ temporary error in fetching logs: %s ]" % ex,
                    "stderr",
                    job_id=self._job.id,
                )

        stdout_tail = S3Tail(stdout_location)
        stderr_tail = S3Tail(stderr_location)

        # 1) Loop until the job has started
        wait_for_launch(self._job)

        # 2) Loop until the job has finished
        start_time = time.time()
        is_running = True
        next_log_update = start_time
        log_update_delay = 1

        while is_running:
            if time.time() > next_log_update:
                _print_available(stdout_tail, "stdout")
                _print_available(stderr_tail, "stderr")
                now = time.time()
                log_update_delay = update_delay(now - start_time)
                next_log_update = now + log_update_delay
                is_running = self._job.is_running

            # This sleep should never delay log updates. On the other hand,
            # we should exit this loop when the task has finished without
            # a long delay, regardless of the log tailing schedule
            d = min(log_update_delay, 5.0)
            select.poll().poll(d * 1000)

        # 3) Fetch remaining logs
        #
        # It is possible that we exit the loop above before all logs have been
        # shown.
        #
        # TODO if we notice AWS Batch failing to upload logs to S3, we can add a
        # HEAD request here to ensure that the file exists prior to calling
        # S3Tail and note the user about truncated logs if it doesn't
        _print_available(stdout_tail, "stdout")
        _print_available(stderr_tail, "stderr")
        # In case of hard crashes (OOM), the final save_logs won't happen.
        # We fetch the remaining logs from AWS CloudWatch and persist them to
        # Amazon S3.
        #
        # TODO: AWS CloudWatch fetch logs

        if self._job.has_failed:
            msg = next(
                msg
                for msg in [
                    self._job.reason,
                    "Task crashed",
                ]
                if msg is not None
            )
            raise KubernetesException(
                "%s. "
                "This could be a transient error. "
                "Use @retry to retry." % msg
            )
        else:
            if self._job.is_running:
                # Kill the job if it is still running by throwing an exception.
                raise KubernetesKilledException("Task failed!")
            echo(
                "Task finished with exit code %s." % self._job.status_code,
                "stderr",
                job_id=self._job.id,
            )