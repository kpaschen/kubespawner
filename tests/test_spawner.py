from asyncio import get_event_loop
from jupyterhub.objects import Hub, Server
from jupyterhub.orm import Spawner
from kubernetes.client.models import (
    V1SecurityContext, V1Container, V1Capabilities, V1Pod
)
from kubespawner import KubeSpawner
from traitlets.config import Config
from unittest.mock import Mock
import json
import logging
import os
import pytest

def sync_wait(future):
    loop = get_event_loop()
    loop.run_until_complete(future)
    return future.result()


class MockUser(Mock):
    name = 'fake'
    server = Server()

    @property
    def escaped_name(self):
        return self.name

    @property
    def url(self):
        return self.server.url

def test_deprecated_config():
    """Deprecated config is handled correctly"""
    with pytest.warns(DeprecationWarning):
        c = Config()
        # both set, non-deprecated wins
        c.KubeSpawner.singleuser_fs_gid = 5
        c.KubeSpawner.fs_gid = 10
        # only deprecated set, should still work
        c.KubeSpawner.hub_connect_ip = '10.0.1.1'
        c.KubeSpawner.singleuser_extra_pod_config = extra_pod_config = {"key": "value"}
        c.KubeSpawner.image_spec = 'abc:123'
        spawner = KubeSpawner(hub=Hub(), config=c, _mock=True)
        assert spawner.hub.connect_ip == '10.0.1.1'
        assert spawner.fs_gid == 10
        assert spawner.extra_pod_config == extra_pod_config
        # deprecated access gets the right values, too
        assert spawner.singleuser_fs_gid == spawner.fs_gid
        assert spawner.singleuser_extra_pod_config == spawner.extra_pod_config
        assert spawner.image == 'abc:123'


def test_deprecated_runtime_access():
    """Runtime access/modification of deprecated traits works"""
    spawner = KubeSpawner(_mock=True)
    spawner.singleuser_uid = 10
    assert spawner.uid == 10
    assert spawner.singleuser_uid == 10
    spawner.uid = 20
    assert spawner.uid == 20
    assert spawner.singleuser_uid == 20
    spawner.image_spec = 'abc:latest'
    assert spawner.image_spec == 'abc:latest'
    assert spawner.image == 'abc:latest'
    spawner.image = 'abc:123'
    assert spawner.image_spec == 'abc:123'
    assert spawner.image == 'abc:123'


def test_spawner_values():
    """Spawner values are set correctly"""
    spawner = KubeSpawner(_mock=True)

    def set_id(spawner):
        return 1

    spawner.uid = 10
    assert spawner.uid == 10
    spawner.uid = set_id
    assert spawner.uid == set_id
    spawner.uid = None
    assert spawner.uid == None

    spawner.gid = 20
    assert spawner.gid == 20
    spawner.gid = set_id
    assert spawner.gid == set_id
    spawner.gid = None
    assert spawner.gid == None

    spawner.fs_gid = 30
    assert spawner.fs_gid == 30
    spawner.fs_gid = set_id
    assert spawner.fs_gid == set_id
    spawner.fs_gid = None
    assert spawner.fs_gid == None


@pytest.mark.asyncio
async def test_spawn(kube_ns, kube_client, config):
    spawner = KubeSpawner(hub=Hub(), user=MockUser(), config=config)
    # empty spawner isn't running
    status = await spawner.poll()
    assert isinstance(status, int)

    # start the spawner
    await spawner.start()
    # verify the pod exists
    pods = kube_client.list_namespaced_pod(kube_ns).items
    pod_names = [p.metadata.name for p in pods]
    assert "jupyter-%s" % spawner.user.name in pod_names
    # verify poll while running
    status = await spawner.poll()
    assert status is None
    # stop the pod
    await spawner.stop()

    # verify pod is gone
    pods = kube_client.list_namespaced_pod(kube_ns).items
    pod_names = [p.metadata.name for p in pods]
    assert "jupyter-%s" % spawner.user.name not in pod_names

    # verify exit status
    status = await spawner.poll()
    assert isinstance(status, int)


@pytest.mark.asyncio
async def test_spawn_pending_pods(kube_ns, kube_client):
    """Spawner can deal with pods that are pending."""
    c = Config()
    # Make a config where the fake-jupyter pod cannot be scheduled so it
    # will always be in a pending state.
    # You can use any setting here that will be impossible to satisfy on the
    # minikube cluster. 
    c.KubeSpawner.node_selector = {'disktype': 'ssd'}
    c.KubeSpawner.namespace = kube_ns
    c.KubeSpawner.start_timeout = 30  # Do not wait very long.
    spawner = KubeSpawner(hub=Hub(), user=MockUser(), config=c)
    with pytest.raises(TimeoutError) as te:
      await spawner.start()
    assert('pod/jupyter-fake did not start' in str(te.value))

    pods = kube_client.list_namespaced_pod(kube_ns).items
    assert pods[0].status.phase == 'Pending'
    status = await spawner.poll()
    # Pending state actually makes poll return None
    assert status is None
    await spawner.stop()

    # verify pod is gone
    pods = kube_client.list_namespaced_pod(kube_ns).items
    pod_names = [p.metadata.name for p in pods]
    assert "jupyter-%s" % spawner.user.name not in pod_names

    # verify exit status
    status = await spawner.poll()
    assert isinstance(status, int)


# A fixture just for patching the spawner for the spawn_watcher_exception test.
# This ensures that the patch will be undone even if the test fails; otherwise
# the patch may stay active and cause subsequent tests to fail.
@pytest.fixture(scope="function")
def patched_spawner(config):
    spawner = KubeSpawner(hub=Hub(), user=MockUser(), config=config)
    real_list_method_name = spawner.pod_reflector.list_method_name
    # This is one way of forcing an exception to be thrown from the watcher,
    # and it could happen if someone were to implement a custom pod reflector.
    spawner.pod_reflector.list_method_name = 'NonExistentMethod'
    yield spawner
    spawner.pod_reflector.list_method_name = real_list_method_name


@pytest.mark.asyncio
async def test_spawn_watcher_exception(kube_ns, kube_client, config, caplog, patched_spawner):
    with pytest.raises(TimeoutError) as te:
        await spawner.start()
    assert te is not None
    records = caplog.record_tuples
    assert ("traitlets", logging.ERROR, 'Error when watching resources, retrying in 0.2s') in records
    assert ("traitlets", logging.CRITICAL, 'Pods reflector failed, halting Hub.') in records
    await spawner.stop()


@pytest.mark.asyncio
async def test_spawn_watcher_reflector_started_twice(kube_ns, kube_client, config):
    spawner = KubeSpawner(hub=Hub(), user=MockUser(), config=config)
    await spawner.start()

    with pytest.raises(ValueError) as ve:
        spawner.pod_reflector.start()
    assert ('Thread watching for resources is already running' in str(ve.value))
    await spawner.stop()


@pytest.mark.asyncio
async def test_spawn_progress(kube_ns, kube_client, config):
    spawner = KubeSpawner(hub=Hub(), user=MockUser(name="progress"), config=config)
    # empty spawner isn't running
    status = await spawner.poll()
    assert isinstance(status, int)

    # start the spawner
    start_future = spawner.start()
    # check progress events
    messages = []
    async for progress in spawner.progress():
        assert 'progress' in progress
        assert isinstance(progress['progress'], int)
        assert 'message' in progress
        assert isinstance(progress['message'], str)
        messages.append(progress['message'])

        # ensure we can serialize whatever we return
        with open(os.devnull, "w") as devnull:
            json.dump(progress, devnull)
    assert 'Started container' in '\n'.join(messages)

    await start_future
    # stop the pod
    await spawner.stop()


def test_get_pod_manifest_tolerates_mixed_input():
    """
    Test that the get_pod_manifest function can handle a either a dictionary or
    an object both representing V1Container objects and that the function
    returns a V1Pod object containing V1Container objects.
    """
    c = Config()

    dict_model = {
        'name': 'mock_name_1',
        'image': 'mock_image_1',
        'command': ['mock_command_1']
    }
    object_model = V1Container(
        name="mock_name_2",
        image="mock_image_2",
        command=['mock_command_2'],
        security_context=V1SecurityContext(
            privileged=True,
            run_as_user=0,
            capabilities=V1Capabilities(add=['NET_ADMIN'])
        )
    )
    c.KubeSpawner.init_containers = [dict_model, object_model]

    spawner = KubeSpawner(config=c, _mock=True)

    # this test ensures the following line doesn't raise an error
    manifest = sync_wait(spawner.get_pod_manifest())

    # and tests the return value's types
    assert isinstance(manifest, V1Pod)
    assert isinstance(manifest.spec.init_containers[0], V1Container)
    assert isinstance(manifest.spec.init_containers[1], V1Container)


_test_profiles = [
    {
        'display_name': 'Training Env - Python',
        'default': True,
        'kubespawner_override': {
            'image': 'training/python:label',
            'cpu_limit': 1,
            'mem_limit': 512 * 1024 * 1024,
            }
    },
    {
        'display_name': 'Training Env - Datascience',
        'kubespawner_override': {
            'image': 'training/datascience:label',
            'cpu_limit': 4,
            'mem_limit': 8 * 1024 * 1024 * 1024,
            }
    },
]


@pytest.mark.asyncio
async def test_user_options_set_from_form():
    spawner = KubeSpawner(_mock=True)
    spawner.profile_list = _test_profiles
    # render the form
    await spawner.get_options_form()
    spawner.user_options = spawner.options_from_form({'profile': [1]})
    assert spawner.user_options == {
        'profile': _test_profiles[1]['display_name'],
    }
    # nothing should be loaded yet
    assert spawner.cpu_limit is None
    await spawner.load_user_options()
    for key, value in _test_profiles[1]['kubespawner_override'].items():
        assert getattr(spawner, key) == value


@pytest.mark.asyncio
async def test_user_options_api():
    spawner = KubeSpawner(_mock=True)
    spawner.profile_list = _test_profiles
    # set user_options directly (e.g. via api)
    spawner.user_options = {'profile': _test_profiles[1]['display_name']}

    # nothing should be loaded yet
    assert spawner.cpu_limit is None
    await spawner.load_user_options()
    for key, value in _test_profiles[1]['kubespawner_override'].items():
        assert getattr(spawner, key) == value


@pytest.mark.asyncio
async def test_default_profile():
    spawner = KubeSpawner(_mock=True)
    spawner.profile_list = _test_profiles
    spawner.user_options = {}
    # nothing should be loaded yet
    assert spawner.cpu_limit is None
    await spawner.load_user_options()
    for key, value in _test_profiles[0]['kubespawner_override'].items():
        assert getattr(spawner, key) == value


@pytest.mark.asyncio
async def test_stop_pod_reflector(kube_ns, kube_client):
    c = Config()
    c.KubeSpawner.namespace = kube_ns
    c.KubeSpawner.start_timeout = 30  # Do not wait very long.
    spawner = KubeSpawner(hub=Hub(), user=MockUser(), config=c)

    # start the spawner
    await spawner.start()

    # Manually stop the pod reflector.
    spawner.pod_reflector.stop()
    assert spawner.pod_reflector._stop_event.is_set()

    # This will make the spawner notice that the pod status isn't updating.
    # The spawner will then restart the pod reflector.
    await spawner.stop()

    # Pod reflector is running again.
    assert not spawner.pod_reflector._stop_event.is_set()


def test_pod_name_no_named_servers():
    c = Config()
    c.JupyterHub.allow_named_servers = False

    user = Config()
    user.name = "user"

    orm_spawner = Spawner()

    spawner = KubeSpawner(config=c, user=user, orm_spawner=orm_spawner, _mock=True)

    assert spawner.pod_name == "jupyter-user"


def test_pod_name_named_servers():
    c = Config()
    c.JupyterHub.allow_named_servers = True

    user = Config()
    user.name = "user"

    orm_spawner = Spawner()
    orm_spawner.name = "server"

    spawner = KubeSpawner(config=c, user=user, orm_spawner=orm_spawner, _mock=True)

    assert spawner.pod_name == "jupyter-user-server"


def test_pod_name_escaping():
    c = Config()
    c.JupyterHub.allow_named_servers = True

    user = Config()
    user.name = "some_user"

    orm_spawner = Spawner()
    orm_spawner.name = "test-server!"

    spawner = KubeSpawner(config=c, user=user, orm_spawner=orm_spawner, _mock=True)

    assert spawner.pod_name == "jupyter-some-5fuser-test-2dserver-21"


def test_pod_name_custom_template():
    c = Config()
    c.JupyterHub.allow_named_servers = False

    user = Config()
    user.name = "some_user"

    pod_name_template = "prefix-{username}-suffix"

    spawner = KubeSpawner(config=c, user=user, pod_name_template=pod_name_template, _mock=True)

    assert spawner.pod_name == "prefix-some-5fuser-suffix"
