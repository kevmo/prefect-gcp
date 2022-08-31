from __future__ import annotations
import datetime
from ast import Delete
from importlib.metadata import metadata
import time
from typing import Any, List, Literal, Optional, Union
from unicodedata import name
from uuid import uuid4
from pydantic import BaseModel, Field, root_validator, validator
from google.api_core.client_options import ClientOptions
from google.oauth2 import service_account
from googleapiclient import discovery
import googleapiclient
from prefect.blocks.core import Block
from prefect.infrastructure.base import Infrastructure, InfrastructureResult
from google.cloud import logging
from google.cloud.logging import TextEntry, ProtobufEntry
from google.cloud.logging import ASCENDING
import pytz
from prefect_gcp.credentials import GcpCredentials
from prefect_gcp.cloud_registry import GoogleCloudRegistry
from prefect.docker import get_prefect_image_name
from anyio.abc import TaskStatus
from prefect.utilities.asyncutils import run_sync_in_worker_thread, sync_compatible

class CloudRunJobResult(InfrastructureResult):
    print("MEOW!")
    """Result from a Cloud Run Job."""
class Job(BaseModel):
    metadata: dict
    spec: dict
    status: dict
    name: str
    ready_condition: dict
    execution_status: dict

    def is_ready(self):
        """See if job is ready to be executed"""
        return self.ready_condition.get("status") == "True"

    def is_finished(self):
        """See if job has a run in progress."""
        return (
            self.execution_status and
            self.execution_status.get("completionTimestamp") is not None
        )

    @staticmethod
    def _get_ready_condition(job):
        ready_condition = {}

        if job["status"].get("conditions"):
            for condition in job["status"]["conditions"]:
                if condition["type"] == 'Ready':
                    ready_condition = condition
        
        return ready_condition

    @staticmethod 
    def _get_execution_status(job):
        if job["status"].get("latestCreatedExecution"):
            return job["status"]["latestCreatedExecution"]
        
        return {}
    
    @classmethod
    def from_json(cls, job):
        """Construct a Job instance from a Jobs JSON response."""

        return cls(
            metadata = job["metadata"],
            spec = job["spec"],
            status = job["status"],
            name = job["metadata"]["name"],
            ready_condition = cls._get_ready_condition(job),
            execution_status = cls._get_execution_status(job)
        )

class Execution(BaseModel):
    name: str
    metadata: dict
    spec: dict
    status: dict
    log_uri: str

    def is_running(self):
        return self.status.get("completionTime") is None

    def final_status(self):
        for condition in self.status["conditions"]:
            if condition["type"] == "Completed":
                return condition

    def succeeded(self):
        completed = self.final_status()
        if completed and completed["status"] == "True":
            return True
        
        return False

    @classmethod
    def from_json(cls, execution):
        return cls(
            name = execution['metadata']['name'],
            metadata = execution['metadata'],
            spec = execution['spec'],
            status = execution['status'],
            log_uri = execution['status'].get('logUri'),
        )

class GoogleCloudLog(BaseModel):
    severity: Optional[str]
    timestamp: datetime.datetime
    insert_id: str
    message: str

    @classmethod
    def from_list_entry(cls, entry):
        # entry_type = entry.payload["@type"]
        if isinstance(entry, TextEntry):
            if entry.payload is None:
                msg = 'Nothing from Text Entry'
            else:
                msg = entry.payload
        elif isinstance(entry, ProtobufEntry):
            if entry.payload.get("status"):
                msg = entry.payload["status"].get("message")
                if entry.payload["response"]["status"].get("completionTime"):
                    print("BINGO!!!!!")
            else:
                msg = "No message"

        return cls(
            severity=entry.severity,
            timestamp=entry.timestamp,
            insert_id=entry.insert_id,
            message=msg
        )


class CloudRunJobLogs(BaseModel):
    job_name: str
    project_id: str
    region: str
    execution_name: str
    poll_interval: int
    credentials: Any
    latest_check: datetime.datetime = None
    last_log_ids: set = {}

    @classmethod
    def from_cloud_run_job(cls, job: CloudRunJob, execution: Execution, poll_interval: int):
        return cls(
            job_name=job.job_name,
            project_id=job.project_id,
            region=job.region,
            execution_name=execution.name,
            credentials=job.credentials,
            poll_interval=poll_interval,
            )

    def get_logs(self):
        raw_logs = self._get_raw_execution_logs()
        new_logs = self._handle_duplicate_logs(raw_logs)
        return [
            GoogleCloudLog.from_list_entry(entry)
            for entry in new_logs
        ]
    
    def _handle_duplicate_logs(self, logs):
        new_logs = []
        for log in logs:
            if log.insert_id not in self.last_log_ids:
                new_logs.append(log)
        
        self.last_log_ids = {log.insert_id for log in new_logs}
        return new_logs

    def _get_raw_execution_logs(self, interval=3):
        logging_client = logging.Client(
            project=self.project_id, 
            credentials=self.credentials.get_credentials_from_service_account(),
            )

        # filter = (
        #     'resource.type:cloud_run_job and '
        #     f'resource.labels.job_name:{self.job_name} and '
        #     # f'resource.labels.location:{self.region} and '
        #     # f'labels."run.googleapis.com/execution_name":{self.execution_name}'
        # )
        filter = (
            f'resource.labels.job_name:{self.job_name}'
            # f'resource.labels.location:{self.region} and '
            # f'labels."run.googleapis.com/execution_name":{self.execution_name}'
        )

        print(filter)
        # if self.latest_check:
        #     # date needs to be wrapped in quotes to filter
        #     # print(f"LATEST CHECK: {self.latest_check}")
        #     filter = filter + f' and timestamp >= "{self.latest_check}"'

        # Make sure that we don't miss logs between gaps
        lookback = 2*self.poll_interval
        time_at_check = datetime.datetime.now(pytz.utc) - datetime.timedelta(seconds=lookback)
        # Time is expected to be in ISO format and in UTC
        iso_time_at_check = time_at_check.strftime("%Y-%m-%dT%H:%M:%SZ")

        entries = logging_client.list_entries(
            resource_names=[f"projects/{self.project_id}"],
            filter_=filter,
            order_by=ASCENDING
        )
        entry_list = [e for e in entries]

        self.latest_check = iso_time_at_check
        return entry_list

PREFECT_GCP_CONTAINER_NAME = "prefect"


class CloudRunJobSettings(Block):
    """
    Block that contains optional settings for CloudRunJob infrastructure. It 
    does not include mandatory settings, which are found on the CloudRunJob
    itself.
    """
    args: Optional[List[str]] = None # done
    command: Optional[List[str]] = None # done
    cpu: Optional[Any] = None # done
    labels: Optional[List[str]] = None # done
    memory: Optional[Any] = None # done
    set_cloud_sql_instances: Optional[List] = None # done
    set_env_vars: Optional[dict] = None # done
    set_secrets: Optional[Any] = None #TODO how does this work?
    vpc_connector: Optional[Any] = None #TODO how does this work? # done
    vpc_egress: Optional[Any] = None #TODO how does this work? # done

    def add_container_settings(self, d: dict) -> dict:
        """
        Add settings related to containers for Cloud Run Jobs to a dictionary.

        Includes environment variables, entrypoint command, entrypoint arguments,
        and cpu and memory limits.
        See: https://cloud.google.com/run/docs/reference/rest/v1/Container
        and https://cloud.google.com/run/docs/reference/rest/v1/Container#ResourceRequirements
        """
        d = self._add_env_vars(d)
        d = self._add_resources(d)
        d = self._add_command(d)
        d = self._add_args(d)

        return d

    def add_jobs_metadata_settings(self, d: dict) -> dict:
        """
        Add top-level Jobs metadata settings for Cloud Run Jobs to a dictionary.

        Includes labels.
        See: https://cloud.google.com/static/run/docs/reference/rest/v1/namespaces.jobs
        """
        return self._add_labels(d)

    def add_execution_template_spec_metadata(self, d: dict) -> dict:
        """Add settings related to the ExecutionTemplateSpec for Cloud Run Jobs
        to a dictionary. 

        Includes Cloud SQL Instances, VPC Access connector, and VPC egress.
        See: https://cloud.google.com/static/run/docs/reference/rest/v1/namespaces.jobs#ExecutionTemplateSpec
        """
        d = self._add_cloud_sql_instances(d)
        d = self._add_vpc_connector(d)
        d = self._add_vpc_egress(d)

        return d

    def _add_env_vars(self, d: dict):
        """Add environment variables for a Cloud Run Job.

        See: https://cloud.google.com/run/docs/reference/rest/v1/Container#envvar 
        """
        if self.set_env_vars is not None:
            env = [{"name": k, "value": v} for k,v in self.set_env_vars.items()]
            d["env"] = env
        
        return d

    def _add_resources(self, d: dict):
        """Set specified resources limits for a Cloud Run Job.

        See: https://cloud.google.com/run/docs/reference/rest/v1/Container#ResourceRequirements
        """
        resources = {"limits": {}}
        if self.cpu is not None:
            resources["limits"]["cpu"] = self.cpu
        if self.memory is not None:
            resources["limits"]["memory"] = self.memory
        
        if resources["limits"]:
            d["resources"] = resources
        
        return d

    def _add_command(self, d: dict):
        """Set the command that a container will run for a Cloud Run Job.

        See: https://cloud.google.com/run/docs/reference/rest/v1/Container
        """
        if self.command:
            d["command"] = self.command
        
        return d

    def _add_args(self, d: dict):
        """Set the arguments that will be passed to the entrypoint for a Cloud Run Job.

        See: https://cloud.google.com/run/docs/reference/rest/v1/Container
        """
        if self.args:
            d["args"] = self.args
        
        return d

    def _add_labels(self, d: dict) -> dict:
        """Provide labels to a Cloud Run Job.

        See: https://cloud.google.com/run/docs/reference/rest/v1/ObjectMeta
        """
        if self.labels:
            d["labels"] = self.labels
        
        return d
    
    def _add_cloud_sql_instances(self, d:dict) -> dict:
        """Set Cloud SQL connections for a Cloud Run Job.

        See: https://cloud.google.com/static/run/docs/reference/rest/v1/namespaces.jobs#ExecutionTemplateSpec
        """
        if self.set_cloud_sql_instances is not None:
            d["annotations"]["run.googleapis.com/cloudsql-instances"] = self.set_cloud_sql_instances

        return d

    def _add_vpc_connector(self, d:dict) -> dict:
        """Set a Serverless VPC Access connector for a Cloud Run Job.

        See: https://cloud.google.com/static/run/docs/reference/rest/v1/namespaces.jobs#ExecutionTemplateSpec
        """
        if self.vpc_connector is not None:
            d["annotations"]["run.googleapis.com/vpc-access-connector"] = self.vpc_connector
        
        return d

    def _add_vpc_egress(self, d:dict) -> dict:
        """Set VPC egrees for a Cloud Run Job.

        See: https://cloud.google.com/static/run/docs/reference/rest/v1/namespaces.jobs#ExecutionTemplateSpec
        """
        if self.vpc_egress is not None:
            d["annotations"]["run.googleapis.com/vpc-access-egress"] = self.vpc_egress
        
        return d


class GoogleCloudLog(BaseModel):
    severity: str
    timestamp: datetime.datetime
    insert_id: str
    message: str

    @classmethod
    def from_list_entry(cls, entry):
        return cls(
            severity=entry.severity,
            timestamp=entry.timestamp,
            insert_id=entry.insert_id,
            message=entry.payload["status"].get("message")
        )


class CloudRunJob(Infrastructure):
    """Infrastructure block used to run GCP Cloud Run Jobs.

    Optional settings are available through the CloudRunJobSettings block.
    """
    type: Literal["cloud-run-job"] = Field(
        "cloud-run-job", description="The slug for this task type."
    )
    image: Optional[str] = Field(
        default_factory=get_prefect_image_name,
        description=(
            "The image to use for the Prefect container in the task. If this value is "
            "not null, it will override the value in the task definition. This value "
            "defaults to a Prefect base image matching your local versions."
        ),
    )
    registry: GoogleCloudRegistry


    region: str
    credentials: GcpCredentials

    # Job settings
    cpu: Optional[str] = None # done
    memory: Optional[str] = None # done

    args: Optional[List[str]] = None # done
    command: list[str] = Field(default_factory=list)# done
    env: dict[str, str] = Field(default_factory=dict)
    stream_output: bool = False

    # Cleanup behavior
    keep_job_after_completion: Optional[bool] = False

    # Private stuff that we use
    _job_name: str = None
    _execution: Optional[Execution] = None
    _project_id: str = None

    @property
    def project_id(self):
        if self._project_id is None:
            self._project_id = self.credentials.get_project_id()
        
        return self._project_id

    @validator("image")
    def remove_spaces(cls, value):
        """Deal with sneaky spaces in image names (hard to see on UI)."""
        return value.replace(" ", "")

    @sync_compatible
    async def run(self, task_status: Optional[TaskStatus] = None):
        with self._get_jobs_client() as jobs_client:

            try:
                #TODO check if container actually exists
                self.logger.info(f"Creating Cloud Run Job {self.job_name}")
                print(f"Creating Cloud Run Job {self.job_name}")
                self.create_job(client=jobs_client)
            except Exception as exc:
                self.logger.exception(f"Encountered an unexpected error when creating Cloud Run Job {self.job_name}:\n{exc!r}")

            self._wait_for_job_creation(client=jobs_client)

            try:
                self.logger.info(f"Submitting Cloud Run Job {self.job_name} for execution.")
                print(f"Submitting Cloud Run Job {self.job_name} for execution.")
                job_run = self._submit_job_for_execution(client=jobs_client)
            except Exception as exc:
                #TODO self.job_name is doing None
                self.logger.exception(f"Received an unexpected exception when sumbitting Cloud Run Job '{self.job_name}':\n{exc!r}")
                raise

            self.logger.info(
                f"Cloud Run Job {self.name!r}: Running command {' '.join(self.command)!r} "
                f"in container {PREFECT_GCP_CONTAINER_NAME!r} ({self.image})..."
            )
            print(
                f"Cloud Run Job {self.name!r}: Running command {' '.join(self.command)!r} "
                f"in container {self.image}..."
            )

            try:
                job_run = self._watch_job_run(job_run=job_run)
            except Exception as exc:
                self.logger.exception(f"Received an unexpected exception while monitoring Cloud Run Job '{self.job_name}':\n{exc!r}")
                raise

        if job_run.succeeded():
            status_code = 0
            self.logger.info(f"Job Run {self.job_name} completed successfully")
            print(f"Job Run {self.job_name} completed successfully")
        else:
            status_code = 1
            self.logger.error(f"Job Run {self.job_name} did not complete successfully. {job_run.final_status()['message']}")
            print(f"Job Run {self.job_name} did not complete successfully. {job_run.final_status()['message']}")

        self.logger.info(f"Job Run logs can be found on GCP at: {job_run.log_uri}") 
        print(f"Job Run logs can be found on GCP at: {job_run.log_uri}") 

        if not self.keep_job_after_completion:
            try:
                self.logger.info(f"Deleting completed Cloud Run Job {self.job_name} from Google Cloud Run...")
                print(f"Deleting completed Cloud Run Job {self.job_name} from Google Cloud Run...")
                self._delete_job(client=jobs_client)
            except googleapiclient.errors.HttpError as Exc:
                self.logger.exception(f"Received an unexpected exception while attempting to delete completed Cloud Run Job.'{self.job_name}':\n{exc!r}")
        if task_status:
            task_status.started(self.job_name) 

        return await run_sync_in_worker_thread(
            self.get_result,
            self.job_name,
            status_code
        )

    def get_result(self, identifier, status_code):
        return CloudRunJobResult(identifier=identifier, status_code=status_code)

    def create_job(self, client):
        try:
            self._create(client)
        except googleapiclient.errors.HttpError as exc:
            # exc.status_code == 409: 
            self.logger.exception(f"Cloud run job received an unexpected exception when creating Cloud Run Job '{self.job_name}':\n{exc!r}")
            raise

    def _create(self, client):
        request = client.create(
            parent=f"namespaces/{self.project_id}", body=self._body_for_create()
        )
        response = request.execute()
        return response

    def _body_for_create(self):
        """Return the data used in the body for a Job CREATE request.

        See: https://cloud.google.com/run/docs/reference/rest/v1/namespaces.jobs
        """
        jobs_metadata = {
            "name": self.job_name,
            "annotations": {
                # See: https://cloud.google.com/run/docs/troubleshooting#launch-stage-validation
                "run.googleapis.com/launch-stage": "BETA"
            },
        }

        execution_template_spec_metadata = {"annotations": {}}
        # self.settings.add_execution_template_spec_metadata(execution_template_spec_metadata)

        containers = [
            self._add_container_settings({"image": self.image})
        ]
        body = {
            "apiVersion": "run.googleapis.com/v1",
            "kind": "Job",
            "metadata": jobs_metadata,
            "spec": {  # JobSpec
                "template": {  # ExecutionTemplateSpec
                    "metadata": execution_template_spec_metadata,
                    "spec": {  # ExecutionSpec
                        "template": {  # TaskTemplateSpec
                            "spec": {  # TaskSpec
                                "containers": containers
                            }
                        }
                    }
                }
            },
        }
        return body


    def _get_job(self, client) -> Job:
        request = client.get(name=f"namespaces/{self.project_id}/jobs/{self.job_name}")
        res = request.execute()
        return Job.from_json(res)

    def _submit_job_for_execution(self, client):
        request = client.run(
            name=f"namespaces/{self.project_id}/jobs/{self.job_name}"
        )
        response = request.execute()
        return Execution.from_json(response)

    def _get_client(self):
        # region needed for 'v1' API
        api_endpoint = f"https://{self.region}-run.googleapis.com"
        credentials = self.credentials.get_credentials_from_service_account()
        options = ClientOptions(api_endpoint=api_endpoint)

        return (
            discovery.build(
                "run", "v1", client_options=options, credentials=credentials
            )
            .namespaces()
        )

    def _get_jobs_client(self):
        return self._get_client().jobs()


    def _get_executions_client(self):
        return self._get_client().executions()


    def preview(self):
        pass


    def _delete_job(self, client):
        request = client.delete(name=f"namespaces/{self.project_id}/jobs/{self.job_name}")
        response = request.execute()
        return response


    def _watch_job_run(self, job_run: Execution, poll_interval=5):
        client = self._get_executions_client()
        run_log_source = CloudRunJobLogs.from_cloud_run_job(self, job_run, poll_interval=poll_interval)

        # while job_run.is_running():
        while True:
            logs = run_log_source.get_logs()
            if logs:
                for log in logs:
                    self.logger.info(f"{log.timestamp}: {log.message}")
                    print(f"{log.timestamp}: {log.message}")
            time.sleep(poll_interval)

            job_run = Execution.from_json(
                client.get(
                    name=f"namespaces/{job_run.metadata['namespace']}/executions/{job_run.metadata['name']}"
                ).execute()
            )
            # if job_run.log_uri:
            #     print(job_run.log_uri)

        return job_run


    def _wait_for_job_creation(self, client, poll_interval=5):
        """Give created job time to register"""
        job = self._get_job(client=client) 

        while not job.is_ready():
            self.logger.info(f"Job is not yet ready... Current condition: {job.ready_condition}")
            print(f"Job is not yet ready... Current condition: {job.ready_condition}")
            time.sleep(poll_interval)
            job = self._get_job(client=client) 

    def _add_args(self, d: dict):
        """Set the arguments that will be passed to the entrypoint for a Cloud Run Job.

        See: https://cloud.google.com/run/docs/reference/rest/v1/Container
        """
        if self.args:
            d["args"] = self.args
        
        return d

    def _add_command(self, d: dict):
        """Set the command that a container will run for a Cloud Run Job.

        See: https://cloud.google.com/run/docs/reference/rest/v1/Container
        """
        if self.command:
            d["command"] = self.command
        
        return d

    def _add_resources(self, d: dict):
        """Set specified resources limits for a Cloud Run Job.

        See: https://cloud.google.com/run/docs/reference/rest/v1/Container#ResourceRequirements
        """
        resources = {"limits": {}}
        if self.cpu is not None:
            resources["limits"]["cpu"] = self.cpu
        if self.memory is not None:
            resources["limits"]["memory"] = self.memory
        
        if resources["limits"]:
            d["resources"] = resources
        
        return d

    def _add_env(self, d: dict):
        """Add environment variables for a Cloud Run Job.

        See: https://cloud.google.com/run/docs/reference/rest/v1/Container#envvar 
        """
        if self.env is not None:
            env = [{"name": k, "value": v} for k,v in self.env.items()]
            d["env"] = env
        
        return d

    def _add_container_settings(self, d: dict) -> dict:
        """
        Add settings related to containers for Cloud Run Jobs to a dictionary.

        Includes environment variables, entrypoint command, entrypoint arguments,
        and cpu and memory limits.
        See: https://cloud.google.com/run/docs/reference/rest/v1/Container
        and https://cloud.google.com/run/docs/reference/rest/v1/Container#ResourceRequirements
        """
        d = self._add_env(d)
        d = self._add_resources(d)
        d = self._add_command(d)
        d = self._add_args(d)

        return d

    @property
    def job_name(self):
        if self._job_name is None:
            components = self.image.split("/")
            #gcr.ip/<project_name>/repo/whatever
            image_name = components[2]
            modified_image_name = image_name.replace((":"),"-").replace(("."),"-") # only alphanumeric and '-' allowed
            self._job_name = f"{modified_image_name}-{uuid4()}"
        
        return self._job_name


if __name__ == "__main__":
    creds = GcpCredentials(service_account_file="creds.json")
    registry = GoogleCloudRegistry(credentials=creds)

    job = CloudRunJob(
        project_id="helical-bongo-360018",
        region="us-east1",
        image="gcr.io/helical-bongo-360018/taylor-made-image",
        credentials=creds,
        keep_job_after_completion=False,
        registry=registry
    )

    job.run()
