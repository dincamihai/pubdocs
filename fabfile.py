from StringIO import StringIO
import subprocess
import json
from functools import wraps
from fabric.api import *
from fabric.contrib.files import exists
from fabric.contrib.console import confirm
from path import path
import imp


def create_sarge_deployer(name, deployer_env):
    from blinker import Namespace
    from blinker.base import symbol

    deployer = imp.new_module('_sarge_deployer.{name}'.format(**locals()))
    deployer.env = deployer_env
    deployer.app_options = {}
    deployer.default_app = None
    deployer.signal_ns = Namespace()
    deployer.install = deployer.signal_ns.signal('install')
    deployer.has_started = deployer.signal_ns.signal('has_started')
    deployer.promote = deployer.signal_ns.signal('promote')
    deployer.will_stop = deployer.signal_ns.signal('will_stop')

    def _func(func):
        setattr(deployer, func.__name__, func)
        return func

    deployer._func = _func

    @deployer._func
    def _task(func):
        @deployer._func
        @task
        @wraps(func)
        def wrapper(*args, **kwargs):
            with settings(**deployer.env):
                return func(*args, **kwargs)

        return wrapper

    @deployer._func
    def quote_json(config):
        return "'" + json.dumps(config).replace("'", "\\u0027") + "'"

    @deployer._func
    def on(signal_name, app_name='ANY'):
        signal = deployer.signal_ns[signal_name]
        def decorator(func):
            def wrapper(*args, **kwargs):
                return func()
            signal.connect(wrapper, symbol(app_name), False)
            return func
        return decorator

    @deployer._func
    def add_application(app_name, **options):
        if deployer.default_app is None:
            deployer.default_app = app_name
        deployer.app_options[app_name] = options

    def _sarge_cmd(cmd):
        return "{sarge_home}/bin/sarge {cmd}".format(cmd=cmd, **env)

    def _sarge(cmd):
        return run(_sarge_cmd(cmd) + ' 2> /dev/null')

    def _new():
        instance_config = {
            'application_name': env['deployer_app_name'],
        }
        instance_config.update(env.get('sarge_instance_config', {}))
        out = _sarge("new " + deployer.quote_json(instance_config))
        sarge_instance = out.strip()
        return sarge_instance

    def _destroy_instance(sarge_instance):
        with settings(sarge_instance=sarge_instance):
            deployer.will_stop.send(symbol(env['deployer_app_name']))
            _sarge("destroy {sarge_instance}".format(**env))

    def _remove_instances(keep=None):
        for other_instance in _instances():
            if other_instance['id'] == keep:
                continue
            with settings(sarge_instance=other_instance['id']):
                app_name = other_instance['meta']['APPLICATION_NAME']
                deployer.will_stop.send(symbol(app_name))
                _destroy_instance(other_instance['id'])

    def _rolling_deploy():
        sarge_instance = _new()
        instance_dir = env['sarge_home'] / sarge_instance
        with settings(sarge_instance=sarge_instance,
                      instance_dir=instance_dir):
            deployer.install.send(symbol(env['deployer_app_name']))
            _sarge("start {sarge_instance}".format(**env))
            deployer.has_started.send(symbol(env['deployer_app_name']))
            if confirm("Deployed {sarge_instance} - make it live?"
                       .format(**locals())):
                deployer.promote.send(symbol(env['deployer_app_name']))
                _remove_instances(keep=env['sarge_instance'])
            else:
                if confirm("Destroy instance {sarge_instance}?".format(**env)):
                    deployer.will_stop.send(symbol(env['deployer_app_name']))
                    _destroy_instance(env['sarge_instance'])

    def _simple_deploy():
        _remove_instances()
        sarge_instance = _new()
        instance_dir = env['sarge_home'] / sarge_instance
        with settings(sarge_instance=sarge_instance,
                      instance_dir=instance_dir):
            deployer.install.send(symbol(env['deployer_app_name']))
            _sarge("start {sarge_instance}".format(**env))
            deployer.has_started.send(symbol(env['deployer_app_name']))
            deployer.promote.send(symbol(env['deployer_app_name']))

    def _instances():
        app_name = env['deployer_app_name']
        for instance in json.loads(_sarge('list'))['instances']:
            if instance['meta']['APPLICATION_NAME'] != app_name:
                continue
            yield instance

    @deployer._task
    def deploy(app_name=None):
        if app_name is None:
            print "Available applications: %r" % deployer.app_options.keys()
            return
        with settings(deployer_app_name=app_name):
            if deployer.app_options[app_name].get('rolling_update', False):
                _rolling_deploy()
            else:
                _simple_deploy()

    @deployer._task
    def shell(sarge_instance=None):
        if sarge_instance is None:
            sarge_instance = deployer.default_app
        open_shell("exec " + _sarge_cmd("run " + sarge_instance))

    @deployer._task
    def supervisorctl():
        open_shell("exec {sarge_home}/bin/supervisorctl".format(**env))

    return deployer


env['use_ssh_config'] = True

SARGE_HOME = path('/var/local/pubdocs')
REDIS_VAR = SARGE_HOME / 'var' / 'pubdocs-redis'
ES_KIT = ('https://github.com/downloads/elasticsearch/'
          'elasticsearch/elasticsearch-0.19.9.tar.gz')
ES_ATTACH_SPEC = 'elasticsearch/elasticsearch-mapper-attachments/1.6.0'
PUBDOCS_CONFIG = {
    'REDIS_SOCKET': REDIS_VAR / 'redis.sock',
    'SENTRY_DSN': ('http://326f1cd02a1b474a9b973f5e2c74d76c'
                         ':cc011e2b752945b6895938893a8fa14a'
                         '@sentry.gerty.grep.ro/3'),
    'PUBDOCS_FILE_REPO': SARGE_HOME / 'var' / 'pubdocs-file-repo',
    'PUBDOCS_LINKS': '/home/alexm/links.txt',
    'ES_HEAP_SIZE': '256m',
    'ES_PATH_DATA': SARGE_HOME / 'var' / 'pubdocs-es-data',
    'PUBDOCS_ES_URL': 'http://localhost:9200',
}


pubdocs = create_sarge_deployer('pubdocs', {
        'host_string': 'gerty',
        'pubdocs_python_bin': '/usr/local/Python-2.7.3/bin/python',
        'sarge_home': SARGE_HOME,
        'pubdocs_venv': SARGE_HOME / 'var' / 'pubdocs-venv',
        'pubdocs_bin': SARGE_HOME / 'var' / 'pubdocs-bin',
        'pubdocs_redis_var': REDIS_VAR,
        'pubdocs_nginx_instance': "pubdocs-{sarge_instance}.gerty.grep.ro",
        'pubdocs_nginx_live': "pubdocs.gerty.grep.ro",
        'pubdocs_es': SARGE_HOME / 'var' / 'pubdocs-es',
        'pubdocs_es_kit': ES_KIT,
        'pubdocs_es_attach_spec': ES_ATTACH_SPEC,
        'pubdocs_es_bin': (SARGE_HOME / 'var' / 'pubdocs-es' /
                           'elasticsearch-0.19.9' / 'bin'),
        'pubdocs_es_data': PUBDOCS_CONFIG['ES_PATH_DATA'],
    })

pubdocs.add_application('web', rolling_update=True)
pubdocs.add_application('worker')
pubdocs.add_application('redis')
pubdocs.add_application('es')

_pubdocs_env = pubdocs.env

_quote_json = pubdocs.quote_json


@task
def configure():
    with settings(**_pubdocs_env):
        etc_app = env['sarge_home'] / 'etc' / 'app'
        run('mkdir -p {etc_app}'.format(**locals()))
        put(StringIO(json.dumps(PUBDOCS_CONFIG, indent=2)),
            str(etc_app / 'config.json'))


@task
def virtualenv():
    with settings(**_pubdocs_env):
        if not exists(env['pubdocs_venv']):
            run("virtualenv '{pubdocs_venv}' "
                "--distribute --no-site-packages "
                "-p '{pubdocs_python_bin}'"
                .format(**env))

        put("requirements.txt", str(env['pubdocs_venv']))
        run("{pubdocs_venv}/bin/pip install "
            "-r {pubdocs_venv}/requirements.txt"
            .format(**env))


@task
def elasticsearch():
    with settings(**_pubdocs_env):
        run("mkdir -p {pubdocs_es}".format(**env))
        with cd(env['pubdocs_es']):
            run("curl -L '{pubdocs_es_kit}' | tar xzf -".format(**env))
            run("{pubdocs_es_bin}/plugin -install '{pubdocs_es_attach_spec}'"
                .format(**env))


@pubdocs.on('install', 'web')
@pubdocs.on('install', 'worker')
def install_flask_app():
    src = subprocess.check_output(['git', 'archive', 'HEAD'])
    put(StringIO(src), str(env['instance_dir'] / '_src.tar'))
    with cd(env['instance_dir']):
        try:
            run("tar xvf _src.tar")
        finally:
            run("rm _src.tar")

    run("mkdir {instance_dir}/instance".format(**env))

    runrc = (
        "source {pubdocs_venv}/bin/activate\n"
    ).format(**env)
    put(StringIO(runrc), str(env['instance_dir'] / '.runrc'))

    app_name = env['deployer_app_name']

    if app_name == 'web':
        put(StringIO("#!/bin/bash\n"
                     "exec python manage.py runfcgi -s fcgi.sock\n"
                     .format(**env)),
            str(env['instance_dir'] / 'server'),
            mode=0755)

    elif app_name == 'worker':
        put(StringIO("#!/bin/bash\n"
                     "export PYTHONPATH=`pwd`\n"
                     "exec celery worker --app=harvest.celery\n"
                     .format(**env)),
            str(env['instance_dir'] / 'server'),
            mode=0755)


@pubdocs.on('has_started', 'web')
def link_nginx(server_name=None):
    if server_name is None:
        server_name = env['pubdocs_nginx_instance'].format(**env)

    instance_dir = env['sarge_home'] / env['sarge_instance']

    nginx_config = {
        'options': {
            'send_timeout': '2m',
            'client_max_body_size': '20m',
            'proxy_buffers': '8 16k',
            'proxy_buffer_size': '32k',
        },
        'urlmap': [
            {'type': 'fcgi', 'url': '/',
             'socket': 'unix:' + instance_dir / 'fcgi.sock'},
            {'type': 'static', 'url': '/static',
             'path': instance_dir / 'static'},
            {'type': 'proxy', 'url': '/static/lib',
             'upstream_url': 'http://grep.ro/quickpub/lib'},
        ],
    }

    quoted_config = _quote_json(nginx_config)
    run('sudo tek-nginx configure {server_name}:80 {quoted_config}'
        .format(**locals()))

    print "nginx: {server_name}".format(**locals())


@pubdocs.on('promote', 'web')
def link_nginx_live():
    link_nginx(server_name=env['pubdocs_nginx_live'])


@pubdocs.on('will_stop', 'web')
def unlink_nginx():
    server_name = env['pubdocs_nginx_instance'].format(**env)
    run('sudo tek-nginx delete -f {server_name}:80'.format(**locals()))


@pubdocs.on('install', 'redis')
def install_redis():
    run("mkdir -p {pubdocs_redis_var}".format(**env))
    put(StringIO("daemonize no\n"
                 "port 0\n"
                 "unixsocket {pubdocs_redis_var}/redis.sock\n"
                 "dir {pubdocs_redis_var}\n"
                 "loglevel notice\n"
                 .format(**env)),
        str(env['instance_dir'] / 'redis.conf'))

    put(StringIO("#!/bin/bash\n"
                 "exec redis-server redis.conf\n"
                 .format(**env)),
        str(env['instance_dir'] / 'server'),
        mode=0755)


@pubdocs.on('install', 'es')
def install_es_runtime():
    run("mkdir -p {pubdocs_es_data}")
    put(StringIO('path.data: "${ES_PATH_DATA}"\n'
                 'network.host: "127.0.0.1"\n'),
        str(env['instance_dir'] / 'elasticsearch.yml'))

    put(StringIO("#!/bin/bash\n"
                 "exec {pubdocs_es_bin}/elasticsearch -f "
                    "-Des.config=`pwd`/elasticsearch.yml\n"
                 .format(**env)),
        str(env['instance_dir'] / 'server'),
        mode=0755)


@pubdocs.on('install', 'worker')
def install_worker_cronjob():
    put(StringIO("#!/usr/bin/env python\n"
                 "import work\n"
                 "work.download_page.delay()\n"
                 .format(**env)),
        str(env['instance_dir'] / 'cronjob'),
        mode=0755)

    run("mkdir -p {pubdocs_bin}".format(**env))
    put(StringIO("#!/bin/bash\n"
                 "{sarge_home}/bin/sarge run {sarge_instance} ./cronjob\n"
                 .format(**env)),
        str(env['pubdocs_bin'] / 'worker-cron'),
        mode=0755)


# remap tasks to top-level namespace
deploy = pubdocs.deploy
supervisorctl = pubdocs.supervisorctl
shell = pubdocs.shell
del pubdocs
