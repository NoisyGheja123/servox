from __future__ import annotations, print_function

import abc
from abc import abstractclassmethod, abstractmethod
import asyncio
import copy
import enum
import itertools
import os
import traceback
import sys

from pydantic.main import Extra

from pydantic.types import StrictInt, constr
from servo.types import Numeric
import time
from pathlib import Path
from typing import Callable, Collection, List, Optional, Dict, Any, Sequence, Tuple
from kubetest.objects import namespace

from pydantic import BaseModel, ByteSize, Field, FilePath

from servo import (
    Adjustment,
    BaseChecks,
    BaseConfiguration,
    BaseConnector,
    Check,
    Component,
    Control,
    Description,
    Duration,
    DurationProgress,
    License,
    Maturity,
    Setting,
    SettingType,
    connector,
    join_to_series,
    on_event,
    get_hash
)
from kubernetes_asyncio import client, config as kubernetes_asyncio_config, watch
from kubernetes_asyncio.config.kube_config import KUBE_CONFIG_DEFAULT_LOCATION
from kubernetes_asyncio.client.api_client import ApiClient
import loguru
from loguru import logger
from typing import ClassVar, Generator, Mapping, Protocol, Type, Union, cast, get_type_hints, runtime_checkable
from contextlib import asynccontextmanager


# Create a top-level logger for classes that aren't yet passing the connector logger instance around
default_logger = logger.bind(component="kubernetes")


class Condition:
    """A Condition is a convenience wrapper around a function and its arguments
    which allows the function to be called at a later time.

    The function is called in the ``check`` method, which resolves the result to
    a boolean value, thus the condition function should return a boolean or
    something that ultimately resolves to a Truthy or Falsey value.

    Args:
        name: The name of the condition to make it easier to identify.
        fn: The condition function that will be checked.
        *args: Any arguments for the condition function.
        **kwargs: Any keyword arguments for the condition function.

    Attributes:
        name (str): The name of the Condition.
        fn (callable): The condition function that will be checked.
        args (tuple): Arguments for the checking function.
        kwargs (dict): Keyword arguments for the checking function.
        last_check (bool): Holds the state of the last condition check.

    Raises:
        ValueError: The given ``fn`` is not callable.
    """

    def __init__(self, name: str, fn: Callable, *args, **kwargs) -> None:
        if not callable(fn):
            raise ValueError('The Condition function must be callable')

        self.name = name
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

        # last check holds the state of the last check.
        self.last_check = False

    def __str__(self) -> str:
        return f'<Condition (name: {self.name}, met: {self.last_check})>'

    def __repr__(self) -> str:
        return self.__str__()

    async def check(self) -> bool:
        """Check that the condition was met.

        Returns:
            True if the condition was met; False otherwise.
        """
        if asyncio.iscoroutinefunction(self.fn):
            self.last_check = bool(await self.fn(*self.args, **self.kwargs))
        else:
            self.last_check = bool(self.fn(*self.args, **self.kwargs))
        return self.last_check


async def wait_for_condition(
        condition: Condition,
        timeout: int = None,
        interval: Union[int, float] = 1,
        fail_on_api_error: bool = True,
) -> None:
    """Wait for a condition to be met.

    Args:
        condition: The Condition to wait for.
        timeout: The maximum time to wait, in seconds, for the condition to be met.
            If unspecified, this function will wait indefinitely. If specified and
            the timeout is met or exceeded, a TimeoutError will be raised.
        interval: The time, in seconds, to wait before re-checking the condition.
        fail_on_api_error: Fail the condition checks if a Kubernetes API error is
            incurred. An API error can be raised for a number of reasons, including
            a Pod being restarted and temporarily unavailable. Disabling this will
            cause those errors to be ignored, allowing the check to continue until
            timeout or resolution. (default: True).

    Raises:
        TimeoutError: The specified timeout was exceeded.
    """
    default_logger.info(f'waiting for condition: {condition}')

    # define the maximum time to wait. once this is met, we should
    # stop waiting.
    max_time = None
    if timeout is not None:
        max_time = time.time() + timeout

    # start the wait block
    start = time.time()
    while True:
        if max_time and time.time() >= max_time:
            raise TimeoutError(
                f'timed out ({timeout}s) while waiting for condition {condition}'
            )

        # check if the condition is met and break out if it is
        try:
            if await condition.check():
                break
        except client.exceptions.ApiException as e:
            default_logger.warning(f'encountered API exception while waiting: {e}')
            if fail_on_api_error:
                raise

        # if the condition is not met, sleep for the interval
        # to re-check later
        await asyncio.sleep(interval)

    end = time.time()
    default_logger.info(f'wait completed (total={Duration(end-start)}) {condition}')


class ResourceRequirements(enum.Flag):
    """
    The ResourceRequirement enumeration determines how optimization values are submitted to the
    Kubernetes scheduler to allocate core compute resources. Requests establish the lower bounds 
    of the CPU and memory necessary for an application to execute while Limits define the upper
    bounds for resources that can be consumed by a given Pod. The Opsani engine can determine 
    optimal values for these settings by identifying performant, low cost configurations that meet
    target SLOs and/or maximizing performance while identifying the point of diminishing returns
    on further resourcing.
    """
    request = enum.auto()
    limit = enum.auto()
    compute = request | limit

    @property
    def flag(self) -> bool:
        """
        Return a boolean value that indicates if the requirements are an individual flag value.

        The implementation relies on the Python `enum.Flag` modeling of individual members of
        the flag enumeration as values that are powers of two (1, 2, 4, 8, …), while combinations 
        of flags are not.
        """
        value = self.value
        return bool((value & (value - 1) == 0) and value != 0)
    
    @property
    def flags(self) -> bool:
        """
        Return a boolean value that indicates if the requirements are a compoud set of flag values.
        """
        return self.flag is False

    @property    
    def resources_key(self) -> str:
        """
        Return a string value for accessing resource requirements within a Kubernetes Container representation.
        """
        if self == ResourceRequirements.request:
            return "requests"
        elif self == ResourceRequirements.limit:
            return "limits"
        else:
            raise NotImplementedError(f"missing key implementation for resource requirement \"{self}\"")


class Resource(Setting):
    """
    Resource is a class that models Kubernetes specific Setting objects that are subject
    to request and limit configuration.
    """
    requirements: ResourceRequirements = ResourceRequirements.compute

    class Config:
        validate_assignment = True


@runtime_checkable
class KubernetesObj(Protocol):
    """
    KubernetesObj is a protocol that defines the common attributes
    of objects retrieved from the Kubernetes API.
    """

    @property
    def api_version(self) -> str:
        ...
    
    @property
    def kind(self) -> str:
        ...

    @property
    def metadata(self) -> client.V1ObjectMeta:
        ...


class KubernetesModel(abc.ABC):
    """
    KubernetesModel is an abstract base class for Servo connector 
    models that wrap Kubernetes API objects.

    This base class provides common functionality and common object
    properties for all API wrappers. It also defines the following
    abstract methods which all subclasses must implement:

      - ``create``: create the resource on the cluster
      - ``patch``: partially update the resource on the cluster
      - ``delete``: remove the resource from the cluster
      - ``refresh``: refresh the underlying object model
      - ``is_ready``: check if the object is in the ready state

    Args:
         api_object: The underlying Kubernetes API object.

    Attributes:
        obj: The underlying Kubernetes API object.
    """

    obj: KubernetesObj
    '''The underlying Kubernetes API object. Subclasses must update
    the type hint to reflect the type that they are wrapping.
    '''

    api_clients: ClassVar[Dict[str, Type]]
    '''A mapping of all the supported api clients for the API
    object type. Various resources can have multiple versions,
    e.g. "apps/v1", "apps/v1beta1", etc. The preferred version
    for each resource type should be defined under the "preferred"
    key. The preferred API client will be used when the apiVersion
    is not specified for the resource.
    '''

    def __init__(self, obj, logger: loguru.Logger = default_logger, **kwargs) -> None:
        self.obj = obj
        self._logger = logger
    
    def __str__(self) -> str:
        return str(self.obj)

    def __repr__(self) -> str:
        return self.__str__()

    @classmethod
    def obj_type(cls) -> Type:
        """The type of the underlying Kubernetes API object."""
        return get_type_hints(cls)["obj"]

    @property
    def logger(self) -> loguru.Logger:
        """A logger instance for outputting operational messages."""
        return self._logger

    @property
    def api_version(self) -> str:
        """The API version of the Kubernetes object (`obj.apiVersion``)."""
        return self.obj.api_version

    @property
    def name(self) -> str:
        """The name of the Kubernetes object (``obj.metadata.name``)."""
        return cast(str, self.obj.metadata.name)

    @name.setter
    def name(self, name: str):
        """Set the name of the Kubernetes object (``obj.metadata.name``)."""
        self.obj.metadata.name = name

    @property
    def namespace(self) -> str:
        """The namespace of the Kubernetes object (``obj.metadata.namespace``)."""
        return cast(str, self.obj.metadata.namespace)
    
    @namespace.setter
    def namespace(self, namespace: str):
        """Set the namespace of the Kubernetes object (``obj.metadata.namespace``)."""
        self.obj.metadata.namespace = namespace

    @asynccontextmanager
    async def api_client(self) -> Generator[Any]:
        """The API client for the Kubernetes object. This is determined
        by the ``apiVersion`` of the object configuration.

        Raises:
            ValueError: The API version is not supported.
        """
        c = self.api_clients.get(self.api_version)
        # If we didn't find the client in the api_clients dict, use the
        # preferred version.
        if c is None:
            self.logger.warning(
                f'unknown version ({self.api_version}), falling back to preferred version'
            )
            c = self.api_clients.get('preferred')
            if c is None:
                raise ValueError(
                    'unknown version specified and no preferred version '
                    f'defined for resource ({self.api_version})'
                )
        # If we did find it, initialize that client version.
        async with ApiClient() as api:
            yield c(api)
    
    @classmethod
    @asynccontextmanager
    async def preferred_client(cls) -> Generator[Any]:
        """The preferred API client type for the Kubernetes object. This is defined in the
        ``api_clients`` class member dict for each object.

        Raises:
             ValueError: No preferred client is defined for the object.
        """
        c = cls.api_clients.get('preferred')
        if c is None:
            raise ValueError(
                f'no preferred api client defined for object {cls.__name__}',
            )
        async with ApiClient() as api:
            yield c(api)
    
    @abc.abstractclassmethod
    async def read(cls, name: str, namespace: str) -> "KubernetesModel":
        """Read the underlying Kubernetes resource from the cluster and
        return a model instance.

        Args:
            name: The name of the resource to read.
            namespace: The namespace to read the resource from.
        """
    
    @abc.abstractmethod
    async def create(self, namespace: str = None) -> None:
        """Create the underlying Kubernetes resource in the cluster
        under the given namespace.

        Args:
            namespace: The namespace to create the resource under.
                If no namespace is provided, it will use the instance's
                namespace member, which is set when the object is created
                via the kubetest client.
        """
    
    @abc.abstractmethod
    async def patch(self) -> None:
        """Partially update the underlying Kubernetes resource in the cluster.
        """

    @abc.abstractmethod
    async def delete(self, options: client.V1DeleteOptions) -> client.V1Status:
        """Delete the underlying Kubernetes resource from the cluster.

        This method expects the resource to have been loaded or otherwise
        assigned a namespace already. If it has not, the namespace will need
        to be set manually.

        Args:
            options: Options for resource deletion.
        """

    @abc.abstractmethod
    async def refresh(self) -> None:
        """Refresh the local state (``obj``) of the underlying Kubernetes resource."""

    @abc.abstractmethod
    def is_ready(self) -> bool:
        """Check if the resource is in the ready state.

        It is up to the wrapper subclass to define what "ready" means for
        that particular resource.

        Returns:
            True if in the ready state; False otherwise.
        """
    
    # TODO: Add Duration support
    async def wait_until_ready(
            self,
            timeout: int = None,
            interval: Union[int, float] = 1,
            fail_on_api_error: bool = False,
    ) -> None:
        """Wait until the resource is in the ready state.

        Args:
            timeout: The maximum time to wait, in seconds, for the resource
                to reach the ready state. If unspecified, this will wait
                indefinitely. If specified and the timeout is met or exceeded,
                a TimeoutError will be raised.
            interval: The time, in seconds, to wait before re-checking if the
                object is ready.
            fail_on_api_error: Fail if an API error is raised. An API error can
                be raised for a number of reasons, such as 'resource not found',
                which could be the case when a resource is just being started or
                restarted. When waiting for readiness we generally do not want to
                fail on these conditions.

        Raises:
             TimeoutError: The specified timeout was exceeded.
        """
        ready_condition = Condition(
            'api object ready',
            self.is_ready,
        )

        await wait_for_condition(
            condition=ready_condition,
            timeout=timeout,
            interval=interval,
            fail_on_api_error=fail_on_api_error,
        )
    
    # TODO: Add Duration support
    async def wait_until_deleted(self, timeout: int = None, interval: Union[int, float] = 1) -> None:
        """Wait until the resource is deleted from the cluster.

        Args:
            timeout: The maximum time to wait, in seconds, for the resource to
                be deleted from the cluster. If unspecified, this will wait
                indefinitely. If specified and the timeout is met or exceeded,
                a TimeoutError will be raised.
            interval: The time, in seconds, to wait before re-checking if the
                object has been deleted.

        Raises:
            TimeoutError: The specified timeout was exceeded.
        """
        async def deleted_fn():
            try:
                await self.refresh()
            except client.exceptions.ApiException as e:
                # If we can no longer find the deployment, it is deleted.
                # If we get any other exception, raise it.
                if e.status == 404 and e.reason == 'Not Found':
                    return True
                else:
                    self.logger.error('error refreshing object state')
                    raise e
            else:
                # The object was still found, so it has not been deleted
                return False

        delete_condition = Condition(
            'api object deleted',
            deleted_fn
        )

        await wait_for_condition(
            condition=delete_condition,
            timeout=timeout,
            interval=interval,
        )


class Namespace(KubernetesModel):
    """Kubetest wrapper around a Kubernetes `Namespace`_ API Object.

    The actual ``kubernetes.client.V1Namespace`` instance that this
    wraps can be accessed via the ``obj`` instance member.

    This wrapper provides some convenient functionality around the
    API Object and provides some state management for the `Namespace`_.

    .. _Namespace:
        https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.18/#namespace-v1-core
    """

    obj: client.V1Namespace
    api_clients: ClassVar[Dict[str, Type]] = {
        'preferred': client.CoreV1Api,
        'v1': client.CoreV1Api,
    }

    @classmethod
    def new(cls, name: str) -> "Namespace":
        """Create a new Namespace with object backing.

        Args:
            name: The name of the new Namespace.

        Returns:
            A new Namespace instance.
        """
        return cls(obj=client.V1Namespace(
            api_version='v1',
            metadata=client.V1ObjectMeta(
                name=name
            )
        ))
    
    @classmethod
    async def read(cls, name: str) -> "Namespace":
        """Read a Namespace from the Kubernetes API.

        Args:
            name: The name of the Namespace to read.

        Returns:
            A hydrated Namespace instance.
        """
        namespace = cls.new(name)
        await namespace.refresh()
        return namespace

    async def create(self, name: str = None) -> None:
        """Create the Namespace under the given name.

        Args:
            name: The name to create the Namespace under. If the
                name is not provided, it will be assumed to already be
                in the underlying object spec. If it is not, namespace
                operations will fail.
        """
        if name is not None:
            self.name = name

        self.logger.info(f'creating namespace "{self.name}"')
        self.logger.debug(f'namespace: {self.obj}')

        async with self.api_client() as api_client:
            self.obj = await api_client.create_namespace(
                body=self.obj,
            )
    
    async def patch(self) -> None:
        """
        TODO: Add docs....
        """
        async with self.api_client() as api_client:
            await api_client.patch_namespace(
                name=self.name,
                body=self.obj,
            )

    async def delete(self, options: client.V1DeleteOptions = None) -> client.V1Status:
        """Delete the Namespace.

        Args:
             options: Options for Namespace deletion.

        Returns:
            The status of the delete operation.
        """
        if options is None:
            options = client.V1DeleteOptions()

        self.logger.info(f'deleting namespace "{self.name}"')
        self.logger.debug(f'delete options: {options}')
        self.logger.debug(f'namespace: {self.obj}')

        async with self.api_client() as api_client:
            return await api_client.delete_namespace(
                name=self.name,
                body=options,
            )

    async def refresh(self) -> None:
        """Refresh the underlying Kubernetes Namespace resource."""
        async with self.api_client() as api_client:
            self.obj = await api_client.read_namespace(
                name=self.name,
            )

    async def is_ready(self) -> bool:
        """Check if the Namespace is in the ready state.

        Returns:
            True if in the ready state; False otherwise.
        """
        await self.refresh()

        status = self.obj.status
        if status is None:
            return False

        return status.phase.lower() == 'active'

_DEFAULT_SENTINEL = object()

class Container:
    """Kubetest wrapper around a Kubernetes `Container`_ API Object.

    The actual ``kubernetes.client.V1Container`` instance that this
    wraps can be accessed via the ``obj`` instance member.

    This wrapper provides some convenient functionality around the
    API Object and provides some state management for the `Container`_.

    This wrapper does **NOT** subclass the ``objects.ApiObject`` like other
    object wrappers because it is not intended to be created or
    managed from manifest file. It is merely meant to wrap the
    Container spec for a Pod to make Container-targeted actions
    easier.

    .. _Container:
        https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.18/#container-v1-core
    """

    def __init__(self, api_object, pod) -> None:
        self.obj = api_object
        self.pod = pod

    @property
    def name(self) -> str:
        return self.obj.name
    
    @property
    def image(self) -> str:
        """
        Returns the container image name from the underlying container object.
        """
        return self.obj.image

    def get_restart_count(self) -> int:
        """Get the number of times the Container has been restarted.

        Returns:
            The number of times the Container has been restarted.
        """
        container_name = self.obj.name
        pod_status = self.pod.status()

        # If there are no container status, the container hasn't started
        # yet, so there cannot be any restarts.
        if pod_status.container_statuses is None:
            return 0

        for status in pod_status.container_statuses:
            if status.name == container_name:
                return status.restart_count

        raise RuntimeError(
            f'Unable to determine container status for {container_name}'
        )
    
    @property
    def resources(self) -> client.V1ResourceRequirements:
        """
        Return the resource requirements for the Container.

        Returns:
            The Container resource requirements.
        """
        return self.obj.resources

    @resources.setter
    def resources(self, resources: client.V1ResourceRequirements) -> None:
        """
        Set the resource requirements for the Container.

        Args:
            resources: The resource requirements to set.
        """
        self.obj.resources = resources

    def get_resource_requirements(
        self, 
        name: str, 
        requirements: ResourceRequirements = ResourceRequirements.compute, 
        *,
        first: bool = False, 
        reverse: bool = False,
        default: Optional[str] = None
    ) -> Union[str, Tuple[str], None]:
        """
        Retrieve resource requirement values for the Container.

        This method retrieves one or more resource requirement values with a non-exceptional,
        cascading fallback behavior. It is useful for retrieving available requirements from a
        resource that you do not know the configuration of. Requirements are read and returned in        
        declaration order in the `ResourceRequirements` enumeration. Values can be retrieved as a 
        tuple collection or a single value can be returned by setting the `first` argument to `True`. 
        Values are evaluated in `ResourceRequirements` declaration order and the first requirement 
        that contains a string value is returned. Evaluation order can be reversed via the 
        `reverse=True` argument. This is useful is you want to retrieve the limit if one exists or
        else fallback to the request value.

        Args:
            name: The name of the resource to retrieve the requirements of (e.g. "cpu" or "memory").
            requirements: A `ResourceRequirements` flag enumeration specifying the requirements to retrieve.
                Multiple values can be retrieved by using a bitwise or (`|`) operator. Defaults to 
                `ResourceRequirements.compute` which is equal to `ResourceRequirements.request | ResourceRequirements.limit`.
            first: When True, a single value is returned for the first requirement to return a value.
            reverse: When True, the `ResourceRequirements` enumeration evaluated in reverse order.
            default: A default value to return when a resource requirement could not be found.
        
        Returns:
            A tuple of resource requirement strings or `None` values in input order or a singular, optional string 
            value when `first` is True.
        """
        values: List[Union[str, None]] = []
        found_requirements = False
        members = (
            reversed(ResourceRequirements) if reverse else list(ResourceRequirements)
        )

        # iterate all members of the enumeration to support bitwise combinations
        for member in members:
            # skip named combinations of flags
            if member.flags:
                continue

            if requirements & member:
                if not hasattr(self.resources, member.resources_key):
                    raise ValueError(f"unknown resource requirement '{member}'")
                
                requirement_dict: Dict[str, str] = getattr(self.resources, member.resources_key)
                if requirement_dict and name in requirement_dict:
                    value = requirement_dict[name]
                    found_requirements = True

                    if first:
                        return value
                    else:
                        values.append(value)
                
                else:
                    default_logger.warning(f"requirement '{member}' is not set for resource '{name}'")
                    values.append(default)

        if not found_requirements:
            if first:
                # code path only accessible on nothing found due to early exit
                default_logger.debug(f"no resource requirements found. returning default value: {default}")
                return default
            else:
                default_logger.debug(f"no resource requirements found. returning default values: {values}")
        
        return tuple(values)
    
    def set_resource_requirements(
        self, 
        name: str, 
        value: Union[str, Sequence[str]], 
        requirements: ResourceRequirements = ResourceRequirements.compute, 
        *,
        clear_others: bool = False
    ) -> None:
        """
        Set the value for one or more resource requirements on the underlying Container.

        Args:
            name: The resource to set requirements for (e.g. "cpu" or "memory").
            value: The string value or tuple of string values to assign to the resources. Values are 
                assigned in declaration order of members of the `ResourceRequirements` enumeration. If a
                single value is provided, it is assigned to all requirements.
            clear_others: When True, any requirements not specified in the input arguments are cleared.
        """

        values = (
            [value] if isinstance(value, str) else list(value)
        )
        default = values[0]
        for requirement in list(ResourceRequirements):
            # skip named combinations of flags
            if requirement.flags:
                continue

            if not hasattr(self.resources, requirement.resources_key):
                raise ValueError(f"unknown resource requirement '{requirement}'")            
                
            req_dict: Optional[Dict[str, Union[str, None]]] = getattr(self.resources, requirement.resources_key)
            if req_dict is None:
                # we are establishing the first requirements for this resource, hydrate the model
                req_dict = {}
                setattr(self.resources, requirement.resources_key, req_dict)

            if requirement & requirements:
                req_value = (
                    values.pop(0) if len(values) else default
                )
                req_dict[name] = req_value

            else:
                if clear_others:
                    default_logger.debug(f"clearing resource requirement: '{requirement}'")
                    req_dict.pop(name, None)

        
    def __str__(self) -> str:
        return str(self.obj)

    def __repr__(self) -> str:
        return self.__str__()


class Pod(KubernetesModel):
    """Wrapper around a Kubernetes `Pod`_ API Object.

    The actual ``kubernetes.client.V1Pod`` instance that this
    wraps can be accessed via the ``obj`` instance member.

    This wrapper provides some convenient functionality around the
    API Object and provides some state management for the `Pod`_.

    .. _Pod:
        https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.18/#pod-v1-core
    """

    obj: client.V1Pod

    api_clients: ClassVar[Dict[str, Type]] = {
        'preferred': client.CoreV1Api,
        'v1': client.CoreV1Api,
    }

    @classmethod
    async def read(cls, name: str, namespace: str) -> "Pod":
        """Read the Pod from the cluster under the given namespace.

        Args:
            name: The name of the Pod to read.
            namespace: The namespace to read the Pod from.
        """
        default_logger.info(f'reading pod "{name}" in namespace "{namespace}"')

        async with cls.preferred_client() as api_client:
            obj = await api_client.read_namespaced_pod_status(name, namespace)
            default_logger.trace("pod: ", obj)
            return Pod(obj)

    async def create(self, namespace: str = None) -> None:
        """Create the Pod under the given namespace.

        Args:
            namespace: The namespace to create the Pod under.
                If the Pod was loaded via the kubetest client, the
                namespace will already be set, so it is not needed
                here. Otherwise, the namespace will need to be provided.
        """
        if namespace is None:
            namespace = self.namespace

        self.logger.info(f'creating pod "{self.name}" in namespace "{self.namespace}"')
        self.logger.trace(f'pod: {self.obj}')

        async with self.preferred_client() as api_client:
            self.obj = await api_client.create_namespaced_pod(
            namespace=namespace,
            body=self.obj,
        )
    
    async def patch(self) -> None:
        """
        TODO: Add docs....
        """
        self.logger.info(f'patching pod "{self.name}"')
        self.logger.trace(f'pod: {self.obj}')
        async with self.api_client() as api_client:
            await api_client.patch_namespaced_pod(
                name=self.name,
                namespace=self.namespace,
                body=self.obj,
            )

    async def delete(self, options: client.V1DeleteOptions = None) -> client.V1Status:
        """Delete the Pod.

        This method expects the Pod to have been loaded or otherwise
        assigned a namespace already. If it has not, the namespace will
        need to be set manually.

        Args:
            options: Options for Pod deletion.

        Return:
            The status of the delete operation.
        """
        if options is None:
            options = client.V1DeleteOptions()

        self.logger.info(f'deleting pod "{self.name}"')
        self.logger.trace(f'delete options: {options}')
        self.logger.trace(f'pod: {self.obj}')

        async with self.api_client() as api_client:
            return await api_client.delete_namespaced_pod(
                name=self.name,
                namespace=self.namespace,
                body=options,
            )

    async def refresh(self) -> None:
        """Refresh the underlying Kubernetes Pod resource."""
        async with self.api_client() as api_client:
            self.obj = await api_client.read_namespaced_pod_status(
                name=self.name,
                namespace=self.namespace,
            )

    async def is_ready(self) -> bool:
        """Check if the Pod is in the ready state.

        Returns:
            True if in the ready state; False otherwise.
        """
        await self.refresh()

        # if there is no status, the pod is definitely not ready
        status = self.obj.status
        if status is None:
            return False

        # check the pod phase to make sure it is running. a pod in
        # the 'failed' or 'success' state will no longer be running,
        # so we only care if the pod is in the 'running' state.
        phase = status.phase
        if phase.lower() != 'running':
            return False

        # TODO: Check for Ready and ContainersReady (Check if below logic matches)
        # 'Returns bool indicating pod readiness'
        # cont_stats = pod.status.container_statuses
        # conts_ready = cont_stats and len(cont_stats) >= len(pod.spec.containers) and all([cs.ready for cs in pod.status.container_statuses])
        # rdy_conditions = [] if not pod.status.conditions else [con for con in pod.status.conditions if con.type in ['Ready', 'ContainersReady']]
        # pod_ready = len(rdy_conditions) > 1 and all([con.status == 'True' for con in rdy_conditions])
        # return conts_ready and pod_ready
        for cond in status.conditions:
            # we only care about the condition type 'ready'
            if cond.type.lower() != 'ready':
                continue

            # check that the readiness condition is True
            return cond.status.lower() == 'true'

        # Catchall
        return False

    async def get_status(self) -> client.V1PodStatus:
        """Get the status of the Pod.

        Returns:
            The status of the Pod.
        """
        # first, refresh the pod state to ensure latest status
        await self.refresh()

        # return the status of the pod
        return cast(client.V1PodStatus, self.obj.status)
    
    @property
    def containers(self) -> List[Container]:
        """
        Return a list of Container objects from the underlying pod template spec.
        """
        return list(map(lambda c: Container(c, self), self.obj.spec.containers))

    async def get_containers(self) -> List[Container]:
        """Get the Pod's containers.

        Returns:
            A list of containers that belong to the Pod.
        """
        self.logger.info(f'getting containers for pod "{self.name}"')
        await self.refresh()

        return self.containers

    # TODO: Rename `find_container` ??
    def get_container(self, name: str) -> Union[Container, None]:
        """Get a container in the Pod by name.

        Args:
            name (str): The name of the Container.

        Returns:
            Container: The Pod's Container with the matching name. If
            no container with the given name is found, ``None`` is returned.
        """
        return next(filter(lambda c: c.name == name, self.containers))

    async def get_restart_count(self) -> int:
        """Get the total number of Container restarts for the Pod.

        Returns:
            The total number of Container restarts.
        """
        status = await self.get_status()
        if status.container_statuses is None:
            return 0

        total = 0
        for container_status in status.container_statuses:
            total += container_status.restart_count

        return total
    
    async def containers_started(self) -> bool:
        """Check if the Pod's Containers have all started.

        Returns:
            True if all Containers have started; False otherwise.
        """
        # start the flag as true - we will check the state and set
        # this to False if any container is not yet running.
        containers_started = True

        status = await self.get_status()
        if status.container_statuses is not None:
            for container_status in status.container_statuses:
                if container_status.state is not None:
                    if container_status.state.running is not None:
                        if container_status.state.running.started_at is not None:
                            # The container is started, so move on to check the
                            # next container
                            continue
                # If we get here, then the container has not started.
                containers_started = containers_started and False
                break

        return containers_started
    
    def uid(self) -> str:
        """
        Gets the UID for the Pod.

        UID is the unique in time and space value for this object. It is typically generated by the server on successful creation of a resource and is not allowed to change on PUT operations.  Populated by the system. Read-only. More info: http://kubernetes.io/docs/user-guide/identifiers#uids  # noqa: E501
        """
        return self.obj.metadata.uid


class Deployment(KubernetesModel):
    """Kubetest wrapper around a Kubernetes `Deployment`_ API Object.

    The actual ``kubernetes.client.V1Deployment`` instance that this
    wraps can be accessed via the ``obj`` instance member.

    This wrapper provides some convenient functionality around the
    API Object and provides some state management for the `Deployment`_.

    .. _Deployment:
        https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.18/#deployment-v1-apps
    """

    obj: client.V1Deployment
    api_clients: ClassVar[Dict[str, Type]] = {
        'preferred': client.AppsV1Api,
        'apps/v1': client.AppsV1Api,
        'apps/v1beta1': client.AppsV1beta1Api,
        'apps/v1beta2': client.AppsV1beta2Api,
    }

    async def create(self, namespace: str = None) -> None:
        """Create the Deployment under the given namespace.

        Args:
            namespace: The namespace to create the Deployment under.
                If the Deployment was loaded via the kubetest client, the
                namespace will already be set, so it is not needed here.
                Otherwise, the namespace will need to be provided.
        """
        if namespace is None:
            namespace = self.namespace

        self.logger.info(f'creating deployment "{self.name}" in namespace "{self.namespace}"')
        self.logger.debug(f'deployment: {self.obj}')

        async with self.api_client() as api_client:
            self.obj = await api_client.create_namespaced_deployment(
                namespace=namespace,
                body=self.obj,
            )
    
    @classmethod
    async def read(cls, name: str, namespace: str) -> "Deployment":
        """Read a Deployment by name under the given namespace.

        Args:
            name: The name of the Deployment to read.
            namespace: The namespace to read the Deployment from.
        """
        
        async with cls.preferred_client() as api_client:
            obj = await api_client.read_namespaced_deployment(name, namespace)
            return Deployment(obj)
    
    async def patch(self) -> None:
        """Update the changed attributes of the Deployment.
        """
        async with self.api_client() as api_client:
            self.obj = await api_client.patch_namespaced_deployment(
                name=self.name,
                namespace=self.namespace,
                body=self.obj,
            )

    async def delete(self, options: client.V1DeleteOptions = None) -> client.V1Status:
        """Delete the Deployment.

        This method expects the Deployment to have been loaded or otherwise
        assigned a namespace already. If it has not, the namespace will need
        to be set manually.

        Args:
            options: Options for Deployment deletion.

        Returns:
            The status of the delete operation.
        """
        if options is None:
            options = client.V1DeleteOptions()

        self.logger.info(f'deleting deployment "{self.name}"')
        self.logger.debug(f'delete options: {options}')
        self.logger.trace(f'deployment: {self.obj}')

        async with self.api_client() as api_client:
            return await api_client.delete_namespaced_deployment(
                name=self.name,
                namespace=self.namespace,
                body=options,
            )

    async def refresh(self) -> None:
        """Refresh the underlying Kubernetes Deployment resource."""
        async with self.api_client() as api_client:
            self.obj = await api_client.read_namespaced_deployment_status(
                name=self.name,
                namespace=self.namespace,
            )
    
    async def rollback(self) -> None:
        """Roll back an unstable Deployment revision to a previous version."""
        async with ApiClient() as api:
            api_client = client.ExtensionsV1beta1Api(api)
            self.obj = await api_client.create_namespaced_deployment_rollback(
                name=self.name,
                namespace=self.namespace,
                body=self.obj,
            )
    
    async def get_status(self) -> client.V1DeploymentStatus:
        """Get the status of the Deployment.

        Returns:
            The status of the Deployment.
        """
        self.logger.info(f'checking status of deployment "{self.name}"')
        # first, refresh the deployment state to ensure the latest status
        await self.refresh()

        # return the status from the deployment
        return cast(client.V1DeploymentStatus, self.obj.status)

    async def get_pods(self) -> List[Pod]:
        """Get the pods for the Deployment.

        Returns:
            A list of pods that belong to the deployment.
        """
        self.logger.info(f'getting pods for deployment "{self.name}"')
        
        async with Pod.preferred_client() as api_client:
            label_selector = self.obj.spec.selector.match_labels
            pod_list: client.V1PodList = await api_client.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=selector_string(label_selector)
            )

        pods = [Pod(p) for p in pod_list.items]
        return pods
    
    @property
    def status(self) -> client.V1DeploymentStatus:
        """Return the status of the Deployment.

        Returns:
            The status of the Deployment.
        """
        return cast(client.V1DeploymentStatus, self.obj.status)

    @property
    def resource_version(self) -> str:
        """
        Returns the resource version of the Deployment.
        """
        return self.obj.metadata.resource_version
    
    @property
    def observed_generation(self) -> str:
        """
        Returns the observed generation of the Deployment status.

        The generation is observed by the deployment controller.
        """
        return self.obj.status.observed_generation
    
    async def is_ready(self) -> bool:
        """Check if the Deployment is in the ready state.

        Returns:
            True if in the ready state; False otherwise.
        """
        await self.refresh()

        # if there is no status, the deployment is definitely not ready
        status = self.obj.status
        if status is None:
            return False

        # check the status for the number of total replicas and compare
        # it to the number of ready replicas. if the numbers are
        # equal, the deployment is ready; otherwise it is not ready.
        total = status.replicas
        ready = status.ready_replicas

        if total is None:
            return False

        return total == ready
    
    # TODO: Determine if we want this...
    def is_complete(self, target_generation: int) -> bool:
        # Kubernetes marks a Deployment as complete when it has the following characteristics:

        # All of the replicas associated with the Deployment have been updated to the latest version you've specified, meaning any updates you've requested have been completed.
        # All of the replicas associated with the Deployment are available.
        # No old replicas for the Deployment are running.
        ...

    @property
    def containers(self) -> List[Container]:
        """
        Return a list of Container objects from the underlying pod template spec.
        """
        return list(map(lambda c: Container(c, None), self.obj.spec.template.spec.containers))

    def get_container(self, name: str) -> Container:
        """
        Return the container with the given name.
        """
        return next(filter(lambda c: c.name == name, self.containers))

    @property
    def replicas(self) -> int:
        """
        Return the number of desired pods.
        """
        return self.obj.spec.replicas
    
    @replicas.setter
    def replicas(self, replicas: int) -> None:
        """
        Set the number of desired pods.
        """
        self.obj.spec.replicas = replicas
    
    @property
    def label_selector(self) -> str:
        """
        Return a string for matching the Deployment in Kubernetes API calls.
        """
        return selector_string(self.obj.spec.selector.match_labels)
    
    ##
    # Canary support

    @property
    def canary_pod_name(self) -> str:
        """
        Return the name of canary Pod for this Deployment.
        """
        return f"{self.name}-canary"

    async def get_canary_pod(self) -> Pod:
        """
        Retrieve the canary Pod for this Deployment (if any).

        Will raise a Kubernetes API exception if not found.
        """        
        return await Pod.read(self.canary_pod_name, self.namespace)
    

    async def delete_canary_pod(self, *, raise_if_not_found: bool = True, timeout: Numeric = 600) -> Optional[Pod]:
        """
        Delete the canary Pod.
        """
        try:
            canary = await self.get_canary_pod()
            self.logger.warning(f"Deleting canary Pod '{canary.name}' from namespace '{canary.namespace}'...")
            await canary.delete()
            await canary.wait_until_deleted(timeout=timeout)
            self.logger.info(f"Deleted canary Pod '{canary.name}' from namespace '{canary.namespace}'.")
            return canary
        except client.exceptions.ApiException as e:
            if e.status != 404 or e.reason != 'Not Found' and raise_if_not_found:
                raise
        
        return None


    async def ensure_canary_pod(self, *, timeout: Numeric = 600) -> Pod:
        """
        Ensures that a canary Pod exists by deleting and recreating an existing Pod or creating one from scratch.

        TODO: docs...
        """        
        canary_pod_name = self.canary_pod_name
        namespace = self.namespace
        self.logger.debug(f"ensuring existence of canary pod '{canary_pod_name}' based on deployment '{self.name}' in namespace '{namespace}'")
        
        # Delete any pre-existing canary debris
        self.logger.trace("deleting pre-existing canary pod (if any)")
        await self.delete_canary_pod(raise_if_not_found=False, timeout=timeout)
        
        # Setup the canary Pod -- our settings are updated on the underlying PodSpec template
        self.logger.trace(f"building new canary")
        pod_obj = client.V1Pod(metadata=self.obj.spec.template.metadata, spec=self.obj.spec.template.spec)
        pod_obj.metadata.name = canary_pod_name
        pod_obj.metadata.annotations['opsani.com/opsani_tuning_for'] = self.name
        pod_obj.metadata.labels['opsani_role'] = 'tuning'

        canary_pod = Pod(obj=pod_obj)
        canary_pod.namespace = namespace
        self.logger.trace(f"initialized new canary: {canary_pod}")

        # TODO: Attach envoy proxy
        
        # If the servo is running inside Kubernetes, register self as the controller for the Pod and ReplicaSet
        SERVO_POD_NAME = os.environ.get('POD_NAME')
        SERVO_POD_NAMESPACE = os.environ.get('POD_NAMESPACE')
        if SERVO_POD_NAME is not None and SERVO_POD_NAMESPACE is not None:
            self.logger.debug(f"running within Kubernetes, registering as Pod controller... (pod={SERVO_POD_NAME}, namespace={SERVO_POD_NAMESPACE})")
            servo_pod = await Pod.read(SERVO_POD_NAME, SERVO_POD_NAMESPACE)
            pod_controller = next(iter(ow for ow in servo_pod.obj.metadata.owner_references if ow.controller))

            # # TODO: Create a ReplicaSet class...
            async with ApiClient() as api:
                api_client = client.AppsV1Api(api)

                servo_rs: client.V1ReplicaSet = await api_client.read_namespaced_replica_set(name=pod_controller.name, namespace=SERVO_POD_NAMESPACE) # still ephemeral
                rs_controller = next(iter(ow for ow in servo_rs.metadata.owner_references if ow.controller))
                servo_dep: client.V1Deployment = await api_client.read_namespaced_deployment(name=rs_controller.name, namespace=SERVO_POD_NAMESPACE)

            canary_pod.obj.metadata.owner_references = [
                client.V1OwnerReference(
                    api_version=self.api_version,
                    block_owner_deletion=True,
                    controller=True, # Ensures the pod will not be adopted by another controller
                    kind='Deployment',
                    name=servo_dep.metadata.name,
                    uid=servo_dep.metadata.uid
                ) 
            ]

        # Create the Pod and wait for it to get ready
        self.logger.info(f"Creating canary Pod '{canary_pod_name}' in namespace '{namespace}'")
        await canary_pod.create()

        self.logger.info(f"Created canary Pod '{canary_pod_name}' in namespace '{namespace}', waiting for it to become ready...")
        await canary_pod.wait_until_ready(timeout=timeout)

        # TODO: Check for unexpected changes to version, etc.

        await canary_pod.refresh()
        await canary_pod.get_containers()

        return canary_pod


class Millicore(int):
    """
    The Millicore class represents one one-hundreth of a vCPU or hyperthread in Kubernetes.
    """
    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield cls.parse
    
    @classmethod
    def parse(cls, v: StrIntFloat) -> 'Millicore':
        """
        Parse an input value into Millicore units.

        Returns:
            The input value in Millicore units.

        Raises:
            ValueError: Raised if the input cannot be parsed.
        """
        if isinstance(v, str):
            if v[-1] == "m":
                return cls(int(v[:-1]))
            else:
                return cls(int(float(v) * 1000))
        elif isinstance(v, (int, float)):
            return cls(int(v * 1000))
        else:
            raise ValueError("could not parse millicore value")
            
    def __str__(self) -> str:
        return f'{int(self)}m'
    
    def __float__(self) -> float:
        return self / 1000.0
    
    def __eq__(self, other) -> bool:
        if isinstance(other, str):
            return str(self) == other
        elif isinstance(other, float):
            return float(self) == other
        return super().__eq__(other)

    def human_readable(self) -> str:
        return str(self)


class CPU(Resource):
    """
    The CPU class models a Kubernetes CPU resource in Millicore units.
    """
    value: Optional[Millicore]
    min: Millicore
    max: Millicore
    step: Millicore
    name = "cpu"
    type = SettingType.RANGE

    # TODO: Don't allow value outside of range

    def opsani_dict(self) -> dict:
        o_dict = super().opsani_dict()
        
        # normalize values into floats (see Millicore __float__)
        for field in ("min", "max", "step", "value"):
            value = getattr(self, field)
            o_dict["cpu"][field] = float(value) if value is not None else None
        return o_dict


# Gibibyte is the base unit of Kubernetes memory
GiB = 1024 * 1024 * 1024


class ShortByteSize(ByteSize):
    """Kubernetes omits the 'B' suffix for some reason"""
    @classmethod
    def validate(cls, v: StrIntFloat) -> 'ShortByteSize':
        if isinstance(v, str):            
            try:
                return super().validate(v)
            except:
                # Append the byte suffix and retry parsing
                return super().validate(v + "b")
        elif isinstance(v, float):
            v = v * GiB
        return super().validate(v)


class Memory(Resource):
    """
    The Memory class models a Kubernetes Memory resource.
    """
    value: Optional[ShortByteSize]
    min: ShortByteSize
    max: ShortByteSize
    step: ShortByteSize
    name = "memory"
    type = SettingType.RANGE

    def opsani_dict(self) -> dict:
        o_dict = super().opsani_dict()

        # normalize values into floating point Gibibyte units
        for field in ("min", "max", "step", "value"):
            value = getattr(self, field)
            o_dict["memory"][field] = float(value) / GiB if value is not None else None
        return o_dict


class Replicas(Setting):
    """
    The Replicas class models a Kubernetes setting that specifies the number of
    desired Pods running in a Deployment.
    """
    value: Optional[StrictInt]
    min: StrictInt
    max: StrictInt
    step: StrictInt = StrictInt(1)
    name = "replicas"
    type = SettingType.RANGE

    class Config:
        validate_assignment = True


# TODO: The Adjustment needs to marshal value appropriately on ingress
def _qualify(value, unit):
    if unit == "memory":
        return f"{value}Gi"# if value.isnumeric() else value
    elif unit == "cpu":
        return str(Millicore.parse(value))
    elif unit == "replicas":
        return int(float(value))
    return value


class BaseOptimization(abc.ABC, BaseModel):
    """
    BaseOptimization is the base class for concrete implementations of optimization strategies.
    """
    name: str
    timeout: Duration

    # Resources
    # cpu: CPU
    # memory: Memory
    # replicas: Replicas
    # TODO: add env and command

    @abstractclassmethod
    async def create(cls, config: DeploymentConfiguration, *args, **kwargs) -> BaseOptimization:
        """
        """
        ...

    @abstractmethod
    async def adjust(self, adjustment: Adjustment, control: Control = Control()) -> None:
        """
        Adjust a setting on the underlying Deployment/Pod or Container.
        """
        ...
    
    @abstractmethod
    async def apply(self) -> None:
        """
        Apply the adjusted settings to the Kubernetes cluster.
        """
        ...
    
    async def handle_error(self, error: Exception, mode: FailureMode) -> bool:
        """
        Handle an operational failure in accordance with the failure mode configured by the operator.

        Well executed error handling requires context and strategic thinking. The servo base library
        provides a rich set of primitives and patterns for approaching error handling but ultimately
        the experience is reliant on the connector developer who has knowledge of the essential context
        and understands the user needs and expectations.

        The error handling implementation provided in this method handles the general cases out of the
        box and relies on abstract methods (see below) to implement more advanced behaviors such as
        rollback and tear-down.

        Returns:
            A boolean value that indicates if the error was handled.

        Raises:
            NotImplementedError: Raised if there is no handler for a given failure mode. Subclasses
                must filter failure modes before calling the superclass implementation.
        """
        if mode == FailureMode.CRASH:
            raise RuntimeError("an unrecoverable failure occurred while interacting with Kubernetes")
        
        elif mode == FailureMode.IGNORE:
            self.logger.warning(f"ignoring runtime error and continuing: {error}")
            self.logger.opt(exception=error).exception("ignoring Kubernetes error")
            return True
        
        elif mode == FailureMode.ROLLBACK:
            await self.rollback(error)
            return True

        elif mode == FailureMode.DESTROY:
            await self.destroy(error)
            return True

        else:
            raise NotImplementedError(f"missing error handler for failure mode '{mode}'")
    
    @abstractmethod
    async def rollback(self, error: Optional[Exception] = None) -> None:
        """
        Asynchronously roll back the Optimization to a previous known
        good state.

        Args:
            error: An optional exception that contextualizes the cause of the rollback.
        """
        ...
    
    @abstractmethod
    async def destroy(self, error: Optional[Exception] = None) -> None:
        """
        Asynchronously destroy the Optimization.

        Args:
            error: An optional exception that contextualizes the cause of the destruction.
        """
        ...

    @abstractmethod
    def to_components(self) -> List[Component]:
        """
        Return a list of Component representations of the Optimization.

        Components are the canonical representation of optimizations in the Opsani API.
        """
        ...
    
    @property
    def logger(self) -> loguru.Logger:
        return default_logger

    def __hash__(self):
        return hash((self.name, id(self),))    

    class Config:
        arbitrary_types_allowed = True


class DeploymentOptimization(BaseOptimization):
    """
    The DeploymentOptimization class implements an optimization strategy based on directly reconfiguring a Kubernetes
    Deployment and its associated containers.
    """
    deployment_config: DeploymentConfiguration
    deployment: Deployment
    container_config: ContainerConfiguration
    container: Container

    @classmethod
    async def create(cls, config: DeploymentConfiguration, **kwargs) -> 'DeploymentOptimization':
        deployment = await Deployment.read(config.name, config.namespace)

        replicas = config.replicas.copy()
        replicas.value = deployment.replicas

        # FIXME: Currently only supporting one container
        for container_config in config.containers:
            container = deployment.get_container(container_config.name)
            return cls(
                name=f"{deployment.name}/{container.name}",
                deployment_config=config,
                deployment=deployment,
                container_config=container_config,
                container=container,
                **kwargs
            )

    @property
    def cpu(self) -> CPU:
        """
        Return the current CPU setting for the optimization.
        """
        cpu = self.container_config.cpu.copy()
        cpu.value = self.container.get_resource_requirements("cpu", first=True)
        return cpu
    
    @property
    def memory(self) -> Memory:
        """
        Return the current Memory setting for the optimization.
        """
        memory = self.container_config.memory.copy()
        memory.value = self.container.get_resource_requirements("memory", first=True)
        return memory

    @property
    def replicas(self) -> Replicas:
        """
        Return the current Replicas setting for the optimization.
        """
        replicas = self.deployment_config.replicas.copy()
        replicas.value = self.deployment.replicas
        return replicas
    
    async def rollback(self, error: Optional[Exception] = None) -> None:
        """
        Initiates an asynchronous rollback to a previous version of the Deployment.

        Args:
            error: An optional error that triggered the rollback.
        """
        self.logger.info(f"adjustment failed: rolling back deployment... ({error})")
        await asyncio.wait_for(
            asyncio.gather(
                self.deployment.rollback()
            ),
            timeout=self.timeout.total_seconds()
        )
    
    async def destroy(self, error: Optional[Exception] = None) -> None:
        """
        Initiates the asynchronous deletion of the Deployment under optimization.

        Args:
            error: An optional error that triggered the destruction.
        """
        self.logger.info(f"adjustment failed: destroying deployment...")
        await asyncio.wait_for(
            asyncio.gather(
                self.deployment.delete()
            ),
            timeout=self.timeout.total_seconds()
        )

    def to_components(self) -> List[Component]:
        return [
            Component(
                name=self.name,
                settings=[
                    self.cpu,
                    self.memory,
                    self.replicas
                ]
            )
        ]

    def adjust(self, adjustment: Adjustment, control: Control = Control()) -> None:
        """
        Adjust the settings on the Deployment or a component Container.

        Adjustments do not take effect on the cluster until the `apply` method is invoked
        to enable aggregation of related adjustments and asynchronous application.
        """
        name = adjustment.setting_name
        value = _qualify(adjustment.value, name)
        self.logger.trace(f"adjusting {name} to {value}")
        if name in ("cpu", "memory"):
            requirements = getattr(self.container_config, name).requirements
            self.container.set_resource_requirements(name, value, requirements, clear_others=True)

        elif adjustment.setting_name == "replicas":
            self.deployment.replicas = int(value)
            
        else:
            raise RuntimeError(f"failed adjustment of unsupported Kubernetes setting '{adjustment.setting_name}'")

    async def apply(self) -> None:
        """
        Apply changes asynchronously and wait for them to roll out to the cluster.

        Kubernetes deployments orchestrate a number of underlying resources. Awaiting the
        outcome of a deployment change requires observation of the `resource_version` which
        indicates if a given patch actually changed the resource, the `observed_generation`
        which is a value managed by the deployments controller and indicates the effective 
        version of the deployment exclusive of insignificant changes that do not affect runtime
        (such as label updates), and the `conditions` of the deployment status which reflect
        state at a particular point in time. How these elements change during a rollout is 
        dependent on the deployment strategy in effect and its requirementss (max unavailable, 
        surge, etc).

        The logic implemented by this method is as follows:
            - Capture the `resource_version` and `observed_generation`.
            - Patch the underlying Deployment object via the Kubernetes API.
            - Check that `resource_version` has been incremented or return early if nothing has changed.
            - Create a Kubernetes Watch on the Deployment targeted by label selector and resource version.
            - Observe events streamed via the watch.
            - Look for the Deployment to report a Status Condition of `"Progressing"`.
            - Wait for the `observed_generation` to increment indicating that the Deployment is applying our changes.
            - Track the value of the `available_replicas`, `ready_replicas`, `unavailable_replicas`, 
                and `updated_replicas` attributes of the Deployment Status until `available_replicas`,
                `ready_replicas`, and `updated_replicas` are all equal to the value of the `replicas` attribute of
                the Deployment and `unavailable_replicas` is `None`. Return success.
            - Raise an error upon expiration of an adjustment timeout or encountering a Deployment Status Condition
                where `type=Progressing` and `status=False`.

        This method abstracts the details of adjusting a Deployment and returns once the desired
        changes have been fully rolled out to the cluster or an error has been encountered.

        See https://kubernetes.io/docs/concepts/workloads/controllers/deployment/

        # The resource_version attribute lets us efficiently watch for changes
        # reference: https://kubernetes.io/docs/reference/using-api/api-concepts/#efficient-detection-of-changes
        """
        
        # Resource version lets us track any change. Observed generation only increments
        # when the deployment controller sees a significant change that requires rollout
        resource_version = self.deployment.resource_version
        observed_generation = self.deployment.status.observed_generation
        desired_replicas = self.deployment.replicas

        # Patch the Deployment via the Kubernetes API
        await self.deployment.patch()

        # Return fast if nothing was changed
        if self.deployment.resource_version == resource_version:
            self.logger.info(f"adjustments applied to Deployment '{self.deployment.name}' made no changes, continuing")
            return
                
        # Create a Kubernetes watch against the deployment under optimization to track changes
        self.logger.info(f"Using label_selector={self.deployment.label_selector}, resource_version={resource_version}")
        async with client.ApiClient() as api:
            v1 = client.AppsV1Api(api)
            async with watch.Watch().stream(
                v1.list_namespaced_deployment,
                self.deployment.namespace,
                label_selector=self.deployment.label_selector,
                # resource_version=resource_version, # FIXME: The resource version might be expired and fail the watch. Decide if we care
            ) as stream:
                async for event in stream:
                    # NOTE: Event types are ADDED, DELETED, MODIFIED, ERROR
                    event_type, deployment = event['type'], event['object']
                    status: client.V1DeploymentStatus = deployment.status
                    
                    self.logger.debug(f"deployment watch yielded event: {event_type} {deployment.kind} {deployment.metadata.name} in {deployment.metadata.namespace}: {status}")

                    if event_type == 'ERROR':
                        stream.stop()
                        raise RuntimeError(str(deployment))                    

                    # Check that the conditions aren't reporting a failure
                    self._check_conditions(status.conditions)

                    # Early events in the watch may be against previous generation
                    if status.observed_generation == observed_generation:
                        self.logger.debug("observed generation has not changed, continuing watch")
                        continue
                    
                    # Check the replica counts. Once available, updated, and ready match
                    # our expected count and the unavailable count is zero we are rolled out
                    if status.unavailable_replicas:
                        self.logger.debug("found unavailable replicas, continuing watch", status.unavailable_replicas)
                        continue
                    
                    replica_counts = [status.replicas, status.available_replicas, status.ready_replicas, status.updated_replicas]
                    if replica_counts.count(desired_replicas) == len(replica_counts):
                        # We are done: all the counts match. Stop the watch and return
                        self.logger.info("adjustment applied successfully", status)
                        stream.stop()
                        return
    
    def _check_conditions(self, conditions: List[client.V1DeploymentCondition]):
        for condition in conditions:
            if condition.type == "Available":                            
                if condition.status == "True":
                    # If we hit on this and have not raised yet we are good to go
                    break                        
                elif condition.status in ("False", "Unknown"):
                    # Condition has not yet been met, log status and continue monitoring
                    self.logger.debug(f"Condition({condition.type}).status == '{condition.status}' ({condition.reason}): {condition.message}")
                else:
                    raise RuntimeError(f"encountered unexpected Condition status '{condition.status}'")

            elif condition.type == "ReplicaFailure":
                # TODO: Create a specific error type
                raise RuntimeError("ReplicaFailure: message='{condition.status.message}', reason='{condition.status.reason}'")

            elif condition.type == "Progressing":
                if condition.status in ("True", "Unknown"):
                    # Still working
                    self.logger.debug("Deployment update is progressing", condition)
                    break
                if condition.status == "False":
                    # TODO: Create specific error type
                    raise RuntimeError("ProgressionFailure: message='{condition.status.message}', reason='{condition.status.reason}'")
                else:
                    raise AssertionError(f"unknown deployment status condition: {condition.status}")


class CanaryOptimization(BaseOptimization):
    """
    """
    target_deployment: Deployment
    target_deployment_config: DeploymentConfiguration    

    target_container: Container
    target_container_config: ContainerConfiguration

    # Canary Pod may not exist yet
    canary_pod: Pod
    canary_container: Container
    # canary_pod: Optional[Pod]
    # canary_container: Optional[Container]

    @classmethod
    async def create(cls, config: DeploymentConfiguration, **kwargs) -> 'CanaryOptimization':
        deployment = await Deployment.read(config.name, config.namespace.name)

        canary_pod = await deployment.ensure_canary_pod()
        # Retrieve existing canary (if any)
        # TODO: Eliminate the implicit canary behavior, we don't want to create canary as a side-effect
        # try:
        #     canary_pod = await deployment.get_canary_pod()
        #     default_logger.info(f"Found existing canary Pod '{canary_pod.name}' in namespace '{config.namespace.name}'")
        # except client.exceptions.ApiException as e:
        #     canary_pod = None
        #     if e.status != 404 or e.reason != 'Not Found' and raise_if_not_found:
        #         raise

        # FIXME: Currently only supporting one container
        for container_config in config.containers:
            target_container = deployment.get_container(container_config.name)
            canary_container = canary_pod.get_container(container_config.name)

            return cls(
                name=f"{deployment.name}/{canary_container.name}-canary",
                target_deployment_config=config,
                target_deployment=deployment,
                target_container_config=container_config,
                target_container=target_container,
                canary_pod=canary_pod,
                canary_container=canary_container,
                **kwargs
            )
        
        raise AssertionError("deployment configuration must have one or more containers")

    def adjust(self, adjustment: Adjustment, control: Control = Control()) -> None:
        name = adjustment.setting_name
        value = _qualify(adjustment.value, name)

        if name in ("cpu", "memory"):
            requirements = getattr(self.target_container_config, name).requirements
            self.canary_container.set_resource_requirements(name, value, requirements, clear_others=True)
        
        elif name == "replicas":
            default_logger.warning(f'ignoring attempt to set replicas to "{value}" on canary pod "{self.canary_pod.name}"')

        else:
             raise RuntimeError(f"failed adjustment of unsupported Kubernetes setting '{name}'")

    async def apply(self) -> None:
        # FIXME: This is not going to fly for long...
        # pod_obj.spec.containers = [container.obj]
        # debug("BEFORE: ", pod_obj.spec.containers[0].resources, container.resources)
        # pod_obj.spec.containers[0].resources = container.resources
        # debug("AFTER: ", pod_obj.spec.containers[0].resources)

        dep_copy = copy.copy(self.target_deployment)
        dep_copy.obj.spec.resources = self.canary_container.resources
        dep_copy.obj.spec.template.spec.containers[0].resources = self.canary_container.resources

        self.canary = await dep_copy.ensure_canary_pod()

    @property
    def cpu(self) -> CPU:
        """
        Return the current CPU setting for the optimization.
        """
        cpu = self.target_container_config.cpu.copy()
        cpu.value = self.canary_container.get_resource_requirements("cpu", first=True)
        return cpu
    
    @property
    def memory(self) -> Memory:
        """
        Return the current Memory setting for the optimization.
        """
        memory = self.target_container_config.memory.copy()
        memory.value = self.canary_container.get_resource_requirements("memory", first=True)
        return memory

    @property
    def replicas(self) -> Replicas:
        """
        Return the current Replicas setting for the optimization.
        """
        return Replicas(            
            min=0,
            max=1,
            value=1,
        )

    def to_components(self) -> List[Component]:
        """
        Return a Component representation of the canary and its reference target.

        Note that all settings on the target are implicitly pinned because only the canary
        is to be modified during optimization.
        """
        
        # implicitly pin the target settings before we return them
        cpu = self.target_container_config.cpu.copy(update={ "pinned": True })
        if value := self.target_container.get_resource_requirements("cpu", first=True):
            cpu.value = value

        memory = self.target_container_config.memory.copy(update={ "pinned": True })
        if value := self.target_container.get_resource_requirements("memory", first=True):
            memory.value = value

        replicas = self.target_deployment_config.replicas.copy(update={ "pinned": True })
        replicas.value = self.target_deployment.replicas
        
        return [
            Component(
                name=f"{self.target_deployment.name}/{self.target_container.name}",
                settings=[
                    cpu,
                    memory,
                    replicas,
                ]
            ),
            Component(
                name=self.name,
                settings=[
                    self.cpu,
                    self.memory,
                    self.replicas,
                ]
            )
        ]
    
    async def rollback(self, error: Optional[Exception] = None) -> None:
        """
        Not supported. Raises a TypeError when called.

        Rollbacks are not supported by the canary optimization strategy
        because they are dependent on Kubernetes Deployments.
        """
        raise TypeError(
            (
                "rollback is not supported under the canary optimization strategy because rollbacks are applied to "
                "Kubernetes Deployment objects and canary optimization is performed against a standalone Pod."
            )
        )
    
    async def destroy(self, error: Optional[Exception] = None) -> None:
        self.logger.info(f'destroying canary Pod "{self.name}"')
        await self.canary_pod.delete()
        
        self.logger.debug(f'awaiting deletion of canary Pod "{self.name}"')
        await self.canary_pod.wait_until_deleted()

        self.logger.info(f'destroyed canary Pod "{self.name}"')
    
    async def handle_error(self, error: Exception, mode: FailureMode) -> bool:
        if mode == FailureMode.ROLLBACK or mode == FailureMode.DESTROY:
            if mode == FailureMode.ROLLBACK:
                self.logger.warning(f"cannot rollback a canary Pod: falling back to destroy: {error}")
                self.logger.opt(exception=error).exception("")
            
            await asyncio.wait_for(
                self.destroy(),
                timeout=self.timeout.total_seconds()
            )
            return True

        else:
            return await super().handle_error(error, mode)
    
    class Config:
        arbitrary_types_allowed = True
        extra = Extra.allow


class KubernetesOptimizations(BaseModel):
    """
    Models the state of resources under optimization in a Kubernetes cluster.
    """
    config: KubernetesConfiguration
    namespace: Namespace
    optimizations: List[BaseOptimization]
    runtime_id: str
    spec_id: str
    version_id: str

    @classmethod
    async def create(cls, config: KubernetesConfiguration) -> 'KubernetesOptimizations':
        """
        Read the state of all components under optimization from the cluster and return an object representation.
        """
        await config.load_kubeconfig()

        namespace = await Namespace.read(config.namespace)
        optimizations: List[BaseOptimization] = []
        images = {}
        runtime_ids = {}
        pod_tmpl_specs = {}

        for deployment_config in config.deployments:
            if deployment_config.strategy == OptimizationStrategy.DEFAULT:
                optimization = await DeploymentOptimization.create(deployment_config, timeout=config.timeout)
                deployment = optimization.deployment
                container = optimization.container
            elif deployment_config.strategy == OptimizationStrategy.CANARY:
                optimization = await CanaryOptimization.create(deployment_config, timeout=config.timeout)
                deployment = optimization.target_deployment
                container = optimization.target_container
            else:
                raise ValueError(f"unknown optimization strategy: {deployment_config.strategy}")

            optimizations.append(optimization)

            # compile artifacts for checksum calculation
            pods = await deployment.get_pods()
            runtime_ids[optimization.name] = [pod.uid for pod in pods]
            pod_tmpl_specs[deployment.name] = deployment.obj.spec.template.spec
            images[container.name] = container.image
        
        # Compute checksums for change detection
        spec_id = get_hash([pod_tmpl_specs[k] for k in sorted(pod_tmpl_specs.keys())])
        runtime_id = get_hash(runtime_ids)
        version_id = get_hash([images[k] for k in sorted(images.keys())])

        return KubernetesOptimizations(
            config=config,
            namespace=namespace,
            optimizations=optimizations,
            spec_id=spec_id,
            runtime_id=runtime_id,
            version_id=version_id,
        )
    
    def to_components(self) -> List[Component]:
        """
        Return a list of Component objects modeling the state of local optimization activities.

        Components are the canonical representation of systems under optimization. They
        are used for data exchange with the Opsani API
        """
        components = list(map(lambda opt: opt.to_components(), self.optimizations))
        return list(itertools.chain(*components))
    
    def to_description(self) -> Description:
        """
        Return a representation of the current state as a Description object.

        Description objects are used to report state to the Opsani API in order
        to synchronize with the Optimizer service.

        Returns:
            A Description of the current state.
        """                        
        return Description(
            components=self.to_components()
        )
    
    def find_optimization(self, name: str) -> Optional[BaseOptimization]:
        """
        Find and return an optimization by name.
        """
        return next(filter(lambda a: a.name == name, self.optimizations), None)

    async def apply(self, adjustments: List[Adjustment]) -> None:
        """
        Apply a sequence of adjustments and wait for them to take effect on the cluster.
        """
        # Exit early if there is nothing to do
        if not adjustments:
            self.logger.debug("early exiting from adjust: no adjustments")
            return
        
        summary = f"[{', '.join(list(map(str, adjustments)))}]"
        self.logger.info(f"Applying {len(adjustments)} Kubernetes adjustments: {summary}")
        
        # Adjust settings on the local data model
        for adjustment in adjustments:
            if adjustable := self.find_optimization(adjustment.component_name):
                self.logger.info(f"adjusting {adjustment.component_name}: {adjustment}")
                adjustable.adjust(adjustment)
                
            else:
                self.logger.debug(f'ignoring unrecognized adjustment "{adjustment}"')
        
        
        # Apply the changes to Kubernetes and wait for the results
        timeout = self.config.timeout
        if self.optimizations:
            self.logger.debug(f"waiting for adjustments to take effect on {len(self.optimizations)} optimizations")
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(
                        *list(map(lambda a: a.apply(), self.optimizations)),
                        return_exceptions=True
                    ),
                    timeout=timeout.total_seconds()
                )

                for result in results:
                    if isinstance(result, Exception):
                        for optimization in self.optimizations:
                            if await optimization.handle_error(result, self.config.on_failure):
                                # Stop error propogation once it has been handled
                                break

            except asyncio.exceptions.TimeoutError as error:
                self.logger.error(f"timed out after {timeout} waiting for adjustments to apply")
                for optimization in self.optimizations:
                    if await optimization.handle_error(error, self.config.on_failure):
                        # Stop error propogation once it has been handled
                        break
        else:
            self.logger.warning(f"failed to apply adjustments: no adjustables")

        # TODO: Run sanity checks to look for out of band changes
    
    @property
    def logger(self) -> loguru.Logger:
        return default_logger

    class Config:
        arbitrary_types_allowed = True


DNSSubdomainName = constr(strip_whitespace=True, min_length=1, max_length=253, regex="^[0-9a-zA-Z]([0-9a-zA-Z\\.-])*[0-9A-Za-z]$")
"""
DNSSubdomainName models a Kubernetes DNS Subdomain Name used as the name for most resource types.

Valid DNS Subdomain Names conform to [RFC 1123](https://tools.ietf.org/html/rfc1123) and must:
    * contain no more than 253 characters
    * contain only lowercase alphanumeric characters, '-' or '.'
    * start with an alphanumeric character
    * end with an alphanumeric character

See https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#dns-subdomain-names
"""


DNSLabelName = constr(strip_whitespace=True, min_length=1, max_length=63, regex="^[0-9a-zA-Z]([0-9a-zA-Z-])*[0-9A-Za-z]$")
"""
DNSLabelName models a Kubernetes DNS Label Name identified used to name some resource types.

Valid DNS Label Names conform to [RFC 1123](https://tools.ietf.org/html/rfc1123) and must:
    * contain at most 63 characters
    * contain only lowercase alphanumeric characters or '-'
    * start with an alphanumeric character
    * end with an alphanumeric character

See https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#dns-label-names
"""


ContainerTagName = constr(strip_whitespace=True, min_length=1, max_length=128, regex="^[0-9a-zA-Z]([0-9a-zA-Z_\\.\\-/:@])*$") # NOTE: This regex is not a full validation
"""
ContainerTagName models the name of a container referenced in a Kubernetes manifest.

Valid container tags must:
    * be valid ASCII and may contain lowercase and uppercase letters, digits, underscores, periods and dashes. 
    * not start with a period or a dash
    * may contain a maximum of 128 characters
"""


class EnvironmentConfiguration(BaseConfiguration):
    ...


class CommandConfiguration(BaseConfiguration):
    ...


class ContainerConfiguration(BaseConfiguration):
    """
    The ContainerConfiguration class models the configuration of an optimizable container within a Kubernetes Deployment.
    """
    name: ContainerTagName
    command: Optional[str] # TODO: create model...
    cpu: CPU
    memory: Memory
    env: Optional[List[str]] # TODO: create model...


class OptimizationStrategy(str, enum.Enum):
    """
    OptimizationStrategy is an enumeration of the possible ways to perform optimization on a Kubernetes Deployment.
    """
    DEFAULT = "default"
    """The default strategy directly applies adjustments to the target Deployment and its containers.
    """

    CANARY = "canary"
    """The canary strategy creates a servo managed standalone canary Pod based on the target Deployment and makes
    adjustments to it instead of the Deployment itself.
    """


class FailureMode(str, enum.Enum):
    """
    The FailureMode enumeration defines how to handle a failed adjustment of a Kubernetes resource.
    """

    ROLLBACK = "rollback"
    DESTROY = "destroy"
    IGNORE = "ignore"
    CRASH = "crash"

    @classmethod
    def options(cls) -> List[str]:
        """
        Return a list of strings that identifies all failure mode configuration options.
        """
        return list(map(lambda mode: mode.value, cls.__members__.values()))


class BaseKubernetesConfiguration(BaseConfiguration):
    """
    BaseKubernetesConfiguration provides a set of configuration primitives to optimizable Kubernetes resources.

    Child classes of `BaseKubernetesConfiguration` such as the `DeploymentConfiguration` can benefit from
    the cascading configuration behavior implemented on the `KubernetesConfiguration` class.

    Common settings will be cascaded from the containing class for attributes if they have not been explicitly set
    and are equal to the default value. Settings that are mandatory in the superclass (such as timeout and namespace)
    but are available for override should be declared as optional on `BaseKubernetesConfiguration` and overridden and
    declared as mandatory in `BaseKubernetesConfiguration`'.
    """

    kubeconfig: Optional[FilePath] = Field(
        description="Path to the kubeconfig file. If `None`, use the default from the environment.",
    )
    context: Optional[str] = Field(
        description="Name of the kubeconfig context to use."
    )
    namespace: Optional[DNSSubdomainName] = Field(
        description="Kubernetes namespace where the target deployments are running.",
    )
    settlement: Optional[Duration] = Field(
        description="Duration to observe the application after an adjust to ensure the deployment is stable."
    )
    on_failure: FailureMode = Field(
        FailureMode.ROLLBACK,
        description=f"How to handle a failed adjustment. Options are: {join_to_series(list(FailureMode.__members__.values()))}"
    )
    timeout: Optional[Duration] = Field(        
        description="Time interval to wait before considering Kubernetes operations to have failed."
    )


class DeploymentConfiguration(BaseKubernetesConfiguration):
    """
    The DeploymentConfiguration class models the configuration of an optimizable Kubernetes Deployment.
    """
    name: DNSSubdomainName
    containers: List[ContainerConfiguration]
    strategy: OptimizationStrategy = OptimizationStrategy.DEFAULT
    replicas: Replicas


class KubernetesConfiguration(BaseKubernetesConfiguration):
    namespace: DNSSubdomainName = DNSSubdomainName("default")
    timeout: Duration = "5m" # TODO: TypeError: __new__() takes from 1 to 2 positional arguments but 4 were given
  
    deployments: List[DeploymentConfiguration] = Field(
        description="Deployments to be optimized.",
    )

    @classmethod
    def generate(cls, **kwargs) -> "KubernetesConfiguration":
        return cls(
            namespace="default",
            description="Update the namespace, deployment, etc. to match your Kubernetes cluster",
            deployments=[
                DeploymentConfiguration(
                    name="app",
                    replicas=Replicas(
                        min=1,
                        max=2,
                    ),
                    containers=[
                        ContainerConfiguration(
                            name="opsani/co-http:latest",
                            cpu=CPU(
                                min="250m",
                                max="4000m",
                                step="125m"
                            ),
                            memory=Memory(
                                min="256 MiB",
                                max="4.0 GiB",
                                step="128 MiB"
                            )
                        )
                    ]
                )
            ],
            **kwargs
        )
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cascade_common_settings()
        
    def cascade_common_settings(self, *, overwrite: bool = False) -> None:
        """
        Apply common settings to child models that inherit from BaseKubernetesConfiguration.

        This method provides enables hierarchical overrides of common configuration values
        based on shared inheritance. Each attribute is introspected and if it inherits from
        `BaseKubernetesConfiguration`, any common attribute values are copied onto the child
        model, cascading them downward. Only attributes whose value is equal to the default
        and have not been explicitly set are updated.
        """
        for name, field in self.__fields__.items():
            if issubclass(field.type_, BaseKubernetesConfiguration):                
                attribute = getattr(self, name)       
                for obj in (attribute if isinstance(attribute, Collection) else [attribute]):                    
                    for field_name, field in BaseKubernetesConfiguration.__fields__.items():
                        if field_name in BaseConfiguration.__fields__:
                            # don't cascade from the base class
                            continue

                        if field_name in obj.__fields_set__ and not overwrite:
                            default_logger.trace(f"skipping config cascade for unset field '{field_name}'")
                            continue

                        current_value = getattr(obj, field_name)
                        if overwrite or current_value == field.default:
                            parent_value = getattr(self, field_name)
                            setattr(obj, field_name, parent_value)
                            default_logger.trace(f"cascaded setting '{field_name}' from KubernetesConfiguration to child '{attribute}': value={parent_value}")

                        else:
                            default_logger.trace(f"declining to cascade value to field '{field_name}': the default value is set and overwrite is false")
    
    # TODO: This might not be the right home for this method...
    async def load_kubeconfig(self) -> None:
        """
        Asynchronously load the Kubernetes configuration
        """
        config_file = Path(self.kubeconfig or KUBE_CONFIG_DEFAULT_LOCATION).expanduser()
        if config_file.exists():
            await kubernetes_asyncio_config.load_kube_config(
                config_file=str(config_file),
                context=self.context,
            )
        elif os.getenv('KUBERNETES_SERVICE_HOST'):
            kubernetes_asyncio_config.load_incluster_config()
        else:
            raise RuntimeError(f"unable to configure Kubernetes client: no kubeconfig file nor in-cluser environment variables found")

KubernetesOptimizations.update_forward_refs()
DeploymentOptimization.update_forward_refs()
CanaryOptimization.update_forward_refs()

class KubernetesChecks(BaseChecks):
    config: KubernetesConfiguration

    async def check_connectivity(self) -> Check:
        try:
            await KubernetesOptimizations.create(self.config)
        except Exception as e:
            return Check(
                name="Connect to Kubernetes", success=False, comment=str(e)
            )

        return Check(name="Connect to Kubernetes", success=True, comment="")
    
    # TODO: Verify the connectivity & permissions
    # TODO: Check the Deployments exist
    # TODO: Check that the Deployment is available
    # TODO: What other unhealthy conditions?

    # def check_access(self) -> Check:
    #     ...
    
    # def check_deployment_exists(self) -> Check:
    #     ...
    
    # def check_deployment_is_available(self) -> Check:
    #     ...


@connector.metadata(
    description="Kubernetes adjust connector",
    version="1.5.0",
    homepage="https://github.com/opsani/kubernetes-connector",
    license=License.APACHE2,
    maturity=Maturity.EXPERIMENTAL,
)
class KubernetesConnector(BaseConnector):
    config: KubernetesConfiguration

    @on_event()
    async def startup(self) -> None:
        # Ensure we are ready to talk to Kubernetes API
        await self.config.load_kubeconfig()

    @on_event()
    async def describe(self) -> Description:        
        state = await KubernetesOptimizations.create(self.config)
        return state.to_description()

    @on_event()
    async def components(self) -> List[Component]:
        state = await KubernetesOptimizations.create(self.config)
        return state.to_components()
        

    @on_event()
    async def adjust(self, adjustments: List[Adjustment], control: Control = Control()) -> None:
        # TODO: Handle this adjust_on stuff (Do we even need this???)
        # adjust_on = desc.get("adjust_on", False)

        # if adjust_on:
        #     try:
        #         should_adjust = eval(adjust_on, {"__builtins__": None}, {"data": data})
        #     except:
        #         should_adjust = False
        #     if not should_adjust:
        #         return {"status": "ok", "reason": "Skipped due to 'adjust_on' condition"}

        state = await KubernetesOptimizations.create(self.config)
        await state.apply(adjustments)

        # TODO: Move this into event declaration??
        settlement = self.config.settlement
        if settlement:
            self.logger.info(f"Settlement duration of {settlement} requested, sleeping...")            
            progress = DurationProgress(settlement)
            progress_logger = lambda p: self.logger.info(p.annotate("allowing application to settle", False), progress=p.progress)
            await progress.watch(progress_logger)
            self.logger.info(f"Settlement duration of {settlement} has elapsed, resuming optimization.")

    @on_event()
    async def check(self) -> List[Check]:
        return await KubernetesChecks.run(self.config)


def selector_string(selectors: Mapping[str, str]) -> str:
    """Create a selector string from the given dictionary of selectors.

    Args:
        selectors: The selectors to stringify.

    Returns:
        The selector string for the given dictionary.
    """
    return ','.join([f'{k}={v}' for k, v in selectors.items()])


def selector_kwargs(
    fields: Mapping[str, str] = None,
    labels: Mapping[str, str] = None,
) -> Dict[str, str]:
    """Create a dictionary of kwargs for Kubernetes object selectors.

    Args:
        fields: A mapping of fields used to restrict the returned collection of
            Objects to only those which match these field selectors. By default,
            no restricting is done.
        labels: A mapping of labels used to restrict the returned collection of
            Objects to only those which match these label selectors. By default,
            no restricting is done.

    Returns:
        A dictionary that can be used as kwargs for many Kubernetes API calls for
        label and field selectors.
    """
    kwargs = {}
    if fields is not None:
        kwargs['field_selector'] = selector_string(fields)
    if labels is not None:
        kwargs['label_selector'] = selector_string(labels)

    return kwargs


def __todo_encoders():
    # TODO: This is broken atm. Bake ENV support into core
    # TODO: This has dynamic keys to kill off.
    env = component.env
    if env:
        for en, ev in env.items():
            assert isinstance(
                ev, dict
            ), 'Setting "{}" in section "env" of a config file is not a dictionary.'
            if "encoder" in ev:
                for name, setting in describe_encoder(
                    cont_env_dict.get(en),
                    ev["encoder"],
                    exception_context="an environment variable {}" "".format(en),
                ):
                    settings[name] = setting
            if issetting(ev):
                defval = ev.pop("default", None)
                val = cont_env_dict.get(en, defval)
                val = (
                    float(val)
                    if israngesetting(ev) and isinstance(val, (int, str))
                    else val
                )
                assert val is not None, (
                    'Environment variable "{}" does not have a current value defined and '
                    "neither it has a default value specified in a config file. "
                    "Please, set current value for this variable or adjust the "
                    "configuration file to include its default value."
                    "".format(en)
                )
                val = {**ev, "value": val}
                settings[en] = val

            # TODO: Must be added to model...
            # command = comp.get("command")
            # if command:
            #     if command.get("encoder"):
            #         for name, setting in describe_encoder(
            #             cont.get("command", []),
            #             command["encoder"],
            #             exception_context="a command section",
            #         ):
            #             settings[name] = setting
            #         # Remove section "command" from final descriptor
            #     del comp["command"]


             # TODO: Port this
            # command = component.command
            # if command:
            #     if command.get("encoder"):
            #         cont_patch["command"], encoded_settings = encode_encoder(
            #             settings, command["encoder"], expected_type=list
            #         )

            #         # Prevent encoded settings from further processing
            #         for setting in encoded_settings:
            #             del settings[setting]

            # env = component.env
            # if env:
            #     for en, ev in env.items():
            #         if ev.get("encoder"):
            #             val, encoded_settings = encode_encoder(
            #                 settings, ev["encoder"], expected_type=str
            #             )
            #             patch_env = cont_patch.setdefault("env", [])
            #             patch_env.append({"name": en, "value": val})

            #             # Prevent encoded settings from further processing
            #             for setting in encoded_settings:
            #                 del settings[setting]
            #         elif issetting(ev):
            #             patch_env = cont_patch.setdefault("env", [])
            #             patch_env.append({"name": en, "value": str(settings[en]["value"])})
            #             del settings[en]