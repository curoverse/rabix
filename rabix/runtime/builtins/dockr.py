import os
import copy
import signal
import logging
import subprocess

import docker

from rabix import CONFIG
from rabix.common.errors import ResourceUnavailable
from rabix.common.util import handle_signal
from rabix.common.protocol import WrapperJob, Outputs, JobError
from rabix.runtime import from_url, to_json
from rabix.models import App, AppSchema
from rabix.runtime.tasks import Runner
from rabix.common import six

log = logging.getLogger(__name__)
MOUNT_POINT = '/rabix'


class DockerApp(App):
    TYPE = 'app/tool/docker'

    image_ref = property(lambda self: self['docker_image_ref'])
    wrapper_id = property(lambda self: self['wrapper_id'])

    def _validate(self):
        self._check_field('docker_image_ref', dict, null=False)
        self._check_field('wrapper_id', six.string_types, null=False)
        self._check_field('schema', AppSchema, null=False)
        self.schema.validate()


class DockerRunner(Runner):
    """
    Runs docker apps.

    Instantiates a container from specified image, mounts the current directory
    and runs entry point + Cmd + job args (compatibility reasons).
    After running, all Job files are chown-ed to current user.
    A directory is created for each job.
    """

    def __init__(self, task):
        super(DockerRunner, self).__init__(task)
        self.container = None
        self._docker_client = None

    def run(self):
        image_id = get_image(self.docker_client, self.task.app.image_ref)['Id']
        self.container = Container(self.docker_client, image_id,
                                   mount_point=MOUNT_POINT)
        args = (self.task.arguments if self.task.is_replacement
                else self._fix_input_paths(self.task.arguments))
        wrp_job = WrapperJob(self.task.app.wrapper_id,
                             self.task.task_id, args,
                             self.task.resources)
        task_dir = self.task.task_id
        os.mkdir(task_dir)
        in_file = os.path.join(task_dir, '__in__.json')
        out_file = os.path.join(task_dir, '__out__.json')
        with open(in_file, 'w') as fp:
            to_json(wrp_job, fp)
        self.container.run_job(
            '__in__.json', '__out__.json', cwd=task_dir
        ).remove(success_only=True)
        self._fix_uid()
        if not os.path.isfile(out_file):
            raise JobError('Job failed.')
        result = from_url(out_file)
        if isinstance(result, Exception):
            raise result
        return self._fix_output_paths(result)

    def _fix_output_paths(self, result):
        if not isinstance(result, Outputs):
            return result
        mount_point = (MOUNT_POINT if MOUNT_POINT.endswith('/')
                       else MOUNT_POINT + '/')
        for k, v in six.iteritems(result.outputs):
            v = [
                os.path.abspath(os.path.join(mount_point,
                                             self.task.task_id, out))
                for out in v if out
            ]
            for out in v:
                if not out.startswith(mount_point):
                    raise JobError('Output file outside mount point: %s' % out)
            v = [os.path.abspath(out[len(mount_point):]) for out in v]
            result.outputs[k] = v
        return result

    def _fix_input_paths(self, args):
        args = copy.deepcopy(args)
        args['$inputs'] = {
            k: self._transform_input(v)
            for k, v in six.iteritems(args.get('$inputs', {}))
        }
        return args

    def _transform_input(self, inp):
        if not isinstance(inp, list):
            return inp
        cwd = os.path.abspath('.') + '/'
        for i in inp:
            if not i.startswith(cwd):
                raise ValueError('Inputs and outputs must be passed as '
                                 'absolute paths. Got %s' % i)
        return [os.path.join(MOUNT_POINT, i[len(cwd):]) for i in inp]

    def _fix_uid(self):
        fixer_image_id = CONFIG['docker'].get('fixer_image_id') or \
            get_image(self.docker_client, repo='busybox', tag='latest')['Id']
        prefix = self.task.task_id.split('.')[0]
        cmd = [
            '/bin/sh', '-c',
            'chown -R %s:%s %s' % (os.getuid(), os.getegid(), prefix + '.*')
        ]
        c = Container(self.docker_client, fixer_image_id,
                      mount_point=MOUNT_POINT)
        c.run(cmd).wait().remove(success_only=True)

    @property
    def docker_client(self):
        if self._docker_client is None:
            self._docker_client = docker.Client()
        return self._docker_client


class DockerAppInstaller(Runner):
    def run(self):
        get_image(docker.Client(), self.task.app.image_ref)


class Container(object):
    """
    Convenience wrapper around docker container.

    Instantiation of the class does not make the docker container.
    Call run methods to do that.
    """

    def __init__(self, docker_client, image_id, container_config=None,
                 mount_point='/rabix'):
        self.docker = docker_client
        self.base_image_id = image_id
        self.base_cmd = docker_client.inspect_image(image_id)['config']['Cmd']
        self.mount_point = mount_point
        self.config = {
            'Image': self.base_image_id,
            'AttachStdin': False,
            'AttachStdout': False,
            'AttachStderr': False,
            'Tty': False,
            'Privileged': False,
            'Memory': 0,
            'Volumes': {self.mount_point: {}},
            'WorkingDir': self.mount_point,
            'Dns': None
        }
        self.config.update(container_config or {})
        self.binds = {os.path.abspath('.'): self.mount_point}
        self.container = None
        self.image = None

    def _check_container_ready(self):
        if not self.container:
            raise RuntimeError('Container not instantiated yet.')

    def inspect(self):
        self._check_container_ready()
        return self.docker.inspect_container(self.container)

    def is_running(self):
        self._check_container_ready()
        return self.inspect()['State']['Running']

    def wait(self, kill_on=(signal.SIGTERM, signal.SIGINT)):
        self._check_container_ready()

        def handler(signum, _):
            logging.info('Received signal %s, stopping.', signum)
            self.stop()

        with handle_signal(handler, *kill_on):
            self.docker.wait(self.container)
        return self

    def is_success(self):
        self._check_container_ready()
        return self.wait().inspect()['State']['ExitCode'] == 0

    def remove(self, success_only=False):
        self._check_container_ready()
        self.wait()
        if not success_only or self.is_success():
            self.docker.remove_container(self.container)
        return self

    def stop(self, nice=False):
        self._check_container_ready()
        if nice:
            self.docker.stop(self.container)
        else:
            self.docker.kill(self.container)
        return self

    def print_log(self):
        self._check_container_ready()
        if self.is_running():
            for out in self.docker.attach(self.container, stream=True):
                print(out.rstrip())
        else:
            print(self.docker.logs(self.container))
        return self

    def commit(self, message=None, conf=None):
        self._check_container_ready()
        self.image = self.docker.commit(
            self.container['Id'], message=message, conf=conf
        )
        return self

    def run(self, command):
        log.info("Running command %s", command)
        self.container = self.docker.create_container_from_config(
            dict(self.config, Cmd=command)
        )
        self.docker.start(container=self.container, binds=self.binds)
        return self

    def run_and_print(self, command):
        self.run(command)
        self.wait()  # TODO: Remove this line when streaming works.
        return self.print_log()

    def run_job(self, input_path, output_path, cwd=None):
        cmd = self.base_cmd + ['run', '-i', input_path, '-o', output_path]
        if cwd:
            cmd += ['--cwd', cwd]
        return self.run(cmd)

    def schema(self, output=None):
        cmd = self.base_cmd + ['schema']
        cmd += ['--output', output] if output else []
        return self.run_and_print(cmd)


def find_image(client, image_id, repo=None, tag='latest'):
    """Returns image dict if it exists locally, or None"""
    images = client.images()
    img = ([i for i in images if i['Id'].startswith(image_id)]
           if image_id else None)
    if not img:
        img = ([i for i in images if (repo + ':' + tag) in i['RepoTags']]
               if repo and tag else None)
    return (img or [None])[0]


def get_image(client, ref=None, repo=None, tag=None, id=None, pull_attempts=1):
    """Returns the image dict. Pulls from repo if not found locally."""
    ref = ref or {}
    repo = ref.get('image_repo', repo)
    tag = ref.get('image_tag', tag)
    image_id = ref.get('image_id', id)
    if not image_id and not repo:
        raise ValueError('Need either repository or image ID.')
    elif not image_id and not tag:
        log.info('Pulling %s', repo)
        pull = subprocess.Popen(['docker', 'pull', repo])
        pull.wait()
        img = find_image(client, image_id, repo)
        if not img:
            raise ResourceUnavailable(repo, 'Image not found.')
    else:
        log.debug('Searching for image %s:%s ID: %s', repo, tag, image_id)
        img = find_image(client, image_id, repo, tag)
    if img:
        return img

    if pull_attempts < 1:
        uri = ':'.join([repo, tag])
        raise ResourceUnavailable(uri, 'Image not found.')
    if not repo:
        raise ResourceUnavailable(image_id, 'Image not found.')
    log.info('No local image %s:%s. Downloading...', repo, tag)
    pull = subprocess.Popen(['docker', 'pull', ':'.join([repo, tag])])
    pull.wait()
    return get_image(client, ref, pull_attempts - 1)