#!/usr/bin/python3

from charms import layer

from charms.layer import snap

from charms.reactive import endpoint_from_flag
from charms.reactive import when
from charms.reactive import when_any
from charms.reactive import when_not
from charms.reactive import is_state
from charms.reactive import set_state
from charms.reactive import is_flag_set
from charms.reactive import remove_state
from charms.reactive import set_flag
from charms.reactive import clear_flag
from charms.reactive import hook
from charms.reactive import register_trigger
from charms.reactive.helpers import data_changed

from charms.templating.jinja2 import render

from charmhelpers.core.hookenv import config
from charmhelpers.core.hookenv import log
from charmhelpers.core.hookenv import DEBUG

from charmhelpers.core.hookenv import leader_set
from charmhelpers.core.hookenv import leader_get
from charmhelpers.core.hookenv import storage_get

from charmhelpers.core.hookenv import application_version_set
from charmhelpers.core.hookenv import open_port
from charmhelpers.core.hookenv import close_port
from charmhelpers.core.host import write_file
from charmhelpers.core import hookenv
from charmhelpers.core import host
from charmhelpers.contrib.charmsupport import nrpe

from charms.layer import status

from etcdctl import EtcdCtl
from etcdctl import get_connection_string
from etcd_databag import EtcdDatabag
from etcd_lib import (
    get_ingress_address,
    get_ingress_addresses,
    render_grafana_dashboard,
)

from shlex import split
from subprocess import check_call
from subprocess import check_output
from subprocess import CalledProcessError
from shutil import copyfile

import json
import os
import charms.leadership  # noqa
import socket
import time
import traceback
import yaml
import shutil
import random


# Layer Note:   the @when_not etcd.installed state checks are relating to
# a boundry that was superimposed by the etcd-24 release which added support
# for snaps. Snapped etcd is now the only supported mechanism by this charm.
# References to this state will be wiped sometime within the next 10 releases
# of the charm.


# Override the default nagios shortname regex to allow periods, which we
# need because our bin names contain them (e.g. 'snap.foo.daemon'). The
# default regex in charmhelpers doesn't allow periods, but nagios itself does.
nrpe.Check.shortname_re = r'[\.A-Za-z0-9-_]+$'

GRAFANA_DASHBOARD_NAME = 'etcd'

register_trigger(when_not="endpoint.grafana.joined", clear_flag="grafana.configured")
register_trigger(when_not="endpoint.prometheus.joined",
                 clear_flag="prometheus.configured")
register_trigger(when_not="endpoint.prometheus.joined", clear_flag="grafana.configured")


def get_target_etcd_channel():
    """
    Check whether or not etcd is already installed. i.e. we're
    going through an upgrade.  If so, leave the etcd version alone,
    if we're a new install, we can set the default channel here.

    If the user has specified a version, then just return that.

    :return: String snap channel
    """
    channel = hookenv.config('channel')
    if channel == 'auto':
        if snap.is_installed('etcd'):
            return False
        else:
            return '3.4/stable'
    else:
        return channel


@when('etcd.installed')
def snap_upgrade_notice():
    status.blocked('Manual migration required. http://bit.ly/2oznAUZ')


@when_any('etcd.registered', 'etcd.leader.configured')
@when_not('etcd.installed')
@when_not('upgrade.series.in-progress')
def check_cluster_health():
    ''' report on the cluster health every 5 minutes'''
    etcdctl = EtcdCtl()
    health = etcdctl.cluster_health()

    # Determine if the unit is healthy or unhealthy
    if 'unhealthy' in health['status']:
        unit_health = "UnHealthy"
    else:
        unit_health = "Healthy"

    # Determine units peer count, and surface 0 by default
    try:
        peers = len(etcdctl.member_list())
    except Exception:
        unit_health = "Errored"
        peers = 0

    bp = "{0} with {1} known peer{2}"
    status_message = bp.format(unit_health, peers, 's' if peers != 1 else '')

    status.active(status_message)


@when('snap.installed.etcd')
@when_not('etcd.installed')
def set_app_version():
    ''' Surface the etcd application version on juju status '''
    # note - the snap doesn't place an etcd alias on disk. This shall infer
    # the version from etcdctl, as the snap distributes both in lockstep.
    application_version_set(etcd_version())


@when_not('certificates.available')
def missing_relation_notice():
    status.blocked('Missing relation to certificate authority.')


@when('certificates.available')
def prepare_tls_certificates(tls):
    common_name = hookenv.unit_public_ip()
    sans = set()
    sans.add(hookenv.unit_public_ip())
    sans.update(get_ingress_addresses('db'))
    sans.update(get_ingress_addresses('cluster'))
    sans.add(socket.gethostname())

    # add cluster peers as alt names when present
    cluster = endpoint_from_flag('cluster.joined')
    if cluster:
        for ip in cluster.get_db_ingress_addresses():
            sans.add(ip)

    sans = sorted(sans)
    certificate_name = hookenv.local_unit().replace('/', '_')
    tls.request_server_cert(common_name, sans, certificate_name)


@hook('upgrade-charm')
def remove_states():
    # stale state cleanup (pre rev6)
    remove_state('etcd.tls.secured')
    remove_state('etcd.ssl.placed')
    remove_state('etcd.ssl.exported')
    remove_state('etcd.nrpe.configured')
    # force a config re-render in case template changed
    set_state('etcd.rerender-config')


@hook('pre-series-upgrade')
def pre_series_upgrade():
    bag = EtcdDatabag()
    host.service_pause(bag.etcd_daemon)
    status.blocked('Series upgrade in progress')


@hook('post-series-upgrade')
def post_series_upgrade():
    bag = EtcdDatabag()
    host.service_resume(bag.etcd_daemon)


@when('snap.installed.etcd')
@when('leadership.is_leader')
@when_any('config.changed.port', 'config.changed.management_port')
@when_not('etcd.installed')
@when_not('upgrade.series.in-progress')
def leader_config_changed():
    ''' The leader executes the runtime configuration update for the cluster,
    as it is the controlling unit. Will render config, close and open ports and
    restart the etcd service.'''
    configuration = hookenv.config()
    previous_port = configuration.previous('port')
    log('Previous port: {0}'.format(previous_port))
    previous_mgmt_port = configuration.previous('management_port')
    log('Previous management port: {0}'.format(previous_mgmt_port))

    if previous_port and previous_mgmt_port:
        bag = EtcdDatabag()
        etcdctl = EtcdCtl()
        members = etcdctl.member_list()
        # Iterate over all the members in the list.
        for unit_name in members:
            # Grab the previous peer url and replace the management port.
            peer_urls = members[unit_name]['peer_urls']
            log('Previous peer url: {0}'.format(peer_urls))
            old_port = ':{0}'.format(previous_mgmt_port)
            new_port = ':{0}'.format(configuration.get('management_port'))
            url = peer_urls.replace(old_port, new_port)
            # Update the member's peer_urls with the new ports.
            log(etcdctl.member_update(members[unit_name]['unit_id'], url))
        # Render just the leaders configuration with the new values.
        render_config()
        address = get_ingress_address('cluster')
        leader_set({'leader_address':
                   get_connection_string([address],
                                         bag.management_port)})
        host.service_restart(bag.etcd_daemon)


@when('snap.installed.etcd')
@when_not('leadership.is_leader')
@when_any('config.changed.port', 'config.changed.management_port')
@when_not('etcd.installed')
def follower_config_changed():
    ''' Follower units need to render the configuration file, close and open
    ports, and restart the etcd service. '''
    set_state('etcd.rerender-config')


@when('snap.installed.etcd')
@when('config.changed.bind_to_all_interfaces')
@when_not('upgrade.series.in-progress')
def bind_to_all_interfaces_changed():
    set_state('etcd.rerender-config')


@when('etcd.rerender-config')
@when_not('upgrade.series.in-progress')
def rerender_config():
    ''' Config must be updated and service restarted '''
    bag = EtcdDatabag()
    log('Rendering config file for {0}'.format(bag.unit_name))
    render_config()
    if host.service_running(bag.etcd_daemon):
        host.service_restart(bag.etcd_daemon)
    set_app_version()


@when('cluster.joined')
def set_db_ingress_address(cluster):
    ''' Send db ingress address to peers on the cluster relation '''
    address = get_ingress_address('db')
    cluster.set_db_ingress_address(address)


@when('db.connected')
@when('etcd.ssl.placed')
@when('cluster.joined')
def send_cluster_connection_details(cluster, db):
    ''' Need to set the cluster connection string and
    the client key and certificate on the relation object. '''
    cert = read_tls_cert('client.crt')
    key = read_tls_cert('client.key')
    ca = read_tls_cert('ca.crt')
    etcdctl = EtcdCtl()

    # Set the key, cert, and ca on the db relation
    db.set_client_credentials(key, cert, ca)

    port = hookenv.config().get('port')
    # Get all the peers participating in the cluster relation.
    members = cluster.get_db_ingress_addresses()
    # Append our own address to the membership list, because peers dont self
    # actualize
    address = get_ingress_address('db')
    members.append(address)
    members.sort()
    # Create a connection string with all the members on the configured port.
    connection_string = get_connection_string(members, port)
    # Set the connection string on the db relation.
    db.set_connection_string(connection_string, version=etcdctl.version())


@when('db.connected')
@when('etcd.ssl.placed')
@when_not('cluster.joined')
def send_single_connection_details(db):
    ''' '''
    cert = read_tls_cert('client.crt')
    key = read_tls_cert('client.key')
    ca = read_tls_cert('ca.crt')

    etcdctl = EtcdCtl()

    # Set the key and cert on the db relation
    db.set_client_credentials(key, cert, ca)

    bag = EtcdDatabag()
    # Get all the peers participating in the cluster relation.
    address = get_ingress_address('db')
    members = [address]
    # Create a connection string with this member on the configured port.
    connection_string = get_connection_string(members, bag.port)
    # Set the connection string on the db relation.
    db.set_connection_string(connection_string, version=etcdctl.version())


@when('proxy.connected')
@when('etcd.ssl.placed')
@when_any('etcd.leader.configured', 'cluster.joined')
def send_cluster_details(proxy):
    ''' Sends the peer cluster string to proxy units so they can join and act
    on behalf of the cluster. '''
    cert = read_tls_cert('client.crt')
    key = read_tls_cert('client.key')
    ca = read_tls_cert('ca.crt')
    proxy.set_client_credentials(key, cert, ca)

    # format a list of cluster participants
    etcdctl = EtcdCtl()
    peers = etcdctl.member_list()
    cluster = []
    for peer in peers:
        thispeer = peers[peer]
        # Potential member doing registration. Default to skip
        if 'peer_urls' not in thispeer.keys() or not thispeer['peer_urls']:
            continue
        peer_string = "{}={}".format(thispeer['name'], thispeer['peer_urls'])
        cluster.append(peer_string)

    proxy.set_cluster_string(','.join(cluster))


@when('config.changed.channel')
def channel_changed():
    ''' Ensure that the config is updated if the channel changes. '''
    set_state('etcd.rerender-config')


@when('config.changed.channel')
@when_not('etcd.installed')
def snap_install():
    channel = get_target_etcd_channel()
    snap.install('core')
    if channel:
        snap.install('etcd', channel=channel, classic=False)
        remove_state('etcd.ssl.exported')


@when('etcd.ssl.placed')
@when_not('snap.installed.etcd')
def install_etcd():
    ''' Attempt resource get on the "etcd" and "etcdctl" resources. If no
    resources are provided attempt to install from the archive only on the
    16.04 (xenial) series. '''

    if is_state('etcd.installed'):
        msg = 'Manual upgrade required. run-action snap-upgrade.'
        status.blocked(msg)
        return

    status.maintenance('Installing etcd.')

    channel = get_target_etcd_channel()
    if channel:
        snap.install('etcd', channel=channel, classic=False)


@when('snap.installed.etcd')
@when_not('etcd.service-restart.configured')
@when_not('upgrade.series.in-progress')
def add_systemd_restart_always():
    template = 'templates/service-always-restart.systemd-latest.conf'
    service = 'snap.etcd.etcd'

    try:
        # Get the systemd version
        cmd = ['systemd', '--version']
        output = check_output(cmd).decode('UTF-8')
        line = output.splitlines()[0]
        words = line.split()
        assert words[0] == 'systemd'
        systemd_version = int(words[1])

        # Check for old version (for xenial support)
        if systemd_version < 230:
            template = 'templates/service-always-restart.systemd-229.conf'
    except Exception:
        traceback.print_exc()
        hookenv.log('Failed to detect systemd version, using latest template',
                    level='ERROR')

    dest_dir = '/etc/systemd/system/{}.service.d'.format(service)
    os.makedirs(dest_dir, exist_ok=True)
    copyfile(template, '{}/always-restart.conf'.format(dest_dir))
    check_call(['systemctl', 'daemon-reload'])
    host.service_restart('{}.service'.format(service))
    set_state('etcd.service-restart.configured')


@when('snap.installed.etcd')
@when('etcd.ssl.placed')
@when('cluster.joined')
@when_not('leadership.is_leader')
@when_not('etcd.registered')
@when_not('etcd.installed')
@when_not('upgrade.series.in-progress')
def register_node_with_leader(cluster):
    '''
    Control flow mechanism to perform self registration with the leader.

    Before executing self registration, we must adhere to the nature of offline
    static turnup rules. If we find a GUID in the member list without peering
    information the unit will enter a race condition and must wait for a clean
    status output before we can progress to self registration.
    '''
    etcdctl = EtcdCtl()
    bag = EtcdDatabag()
    leader_address = leader_get('leader_address')
    bag.leader_address = leader_address

    try:
        # Check if we are already registered. Unregister ourselves if we are so
        # we can register from scratch.
        peer_url = 'https://%s:%s' % (bag.cluster_address, bag.management_port)
        members = etcdctl.member_list(leader_address)
        for _, member in members.items():
            if member['peer_urls'] == peer_url:
                log('Found member that matches our peer URL. Unregistering...')
                etcdctl.unregister(member['unit_id'], leader_address)

        # Now register.
        resp = etcdctl.register(bag.__dict__)
        bag.set_cluster(resp['cluster'])
    except EtcdCtl.CommandFailed:
        log('etcdctl.register failed, will retry')
        msg = 'Waiting to retry etcd registration'
        status.waiting(msg)
        return

    render_config(bag)
    host.service_restart(bag.etcd_daemon)
    open_port(bag.port)
    set_state('etcd.registered')


@when('etcd.ssl.placed')
@when('leadership.is_leader')
@when_not('etcd.leader.configured')
@when_not('etcd.installed')
@when_not('upgrade.series.in-progress')
def initialize_new_leader():
    ''' Create an initial cluster string to bring up a single member cluster of
    etcd, and set the leadership data so the followers can join this one. '''
    bag = EtcdDatabag()
    bag.token = bag.token
    bag.set_cluster_state('new')
    address = get_ingress_address('cluster')
    cluster_connection_string = get_connection_string([address],
                                                      bag.management_port)
    bag.set_cluster("{}={}".format(bag.unit_name, cluster_connection_string))

    render_config(bag)
    host.service_restart(bag.etcd_daemon)

    # sorry, some hosts need this. The charm races with systemd and wins.
    time.sleep(2)

    # Check health status before we say we are good
    etcdctl = EtcdCtl()
    status = etcdctl.cluster_health()
    if 'unhealthy' in status:
        status.blocked('Cluster not healthy.')
        return
    # We have a healthy leader, broadcast initial data-points for followers
    open_port(bag.port)
    leader_connection_string = get_connection_string([address],
                                                     bag.port)
    leader_set({'leader_address': leader_connection_string,
                'cluster': bag.cluster})

    # set registered state since if we ever become a follower, we will not need
    # to re-register
    set_state('etcd.registered')

    # finish bootstrap delta and set configured state
    set_state('etcd.leader.configured')


@when('snap.installed.etcd')
@when('snap.refresh.set')
@when('leadership.is_leader')
def process_snapd_timer():
    ''' Set the snapd refresh timer on the leader so all cluster members
    (present and future) will refresh near the same time. '''
    # Get the current snapd refresh timer; we know layer-snap has set this
    # when the 'snap.refresh.set' flag is present.
    timer = snap.get(snapname='core', key='refresh.timer').decode('utf-8').strip()
    if not timer:
        # The core snap timer is empty. This likely means a subordinate timer
        # reset ours. Try to set it back to a previously leader-set value,
        # falling back to config if needed. Luckily, this should only happen
        # during subordinate install, so this should remain stable afterward.
        timer = leader_get('snapd_refresh') or hookenv.config('snapd_refresh')
        snap.set_refresh_timer(timer)

        # Ensure we have the timer known by snapd (it may differ from config).
        timer = snap.get(snapname='core', key='refresh.timer').decode('utf-8').strip()

    # The first time through, data_changed will be true. Subsequent calls
    # should only update leader data if something changed.
    if data_changed('etcd_snapd_refresh', timer):
        log('setting snapd_refresh timer to: {}'.format(timer))
        leader_set({'snapd_refresh': timer})


@when('snap.installed.etcd')
@when('snap.refresh.set')
@when('leadership.changed.snapd_refresh')
@when_not('leadership.is_leader')
def set_snapd_timer():
    ''' Set the snapd refresh.timer on non-leader cluster members. '''
    # NB: This method should only be run when 'snap.refresh.set' is present.
    # Layer-snap will always set a core refresh.timer, which may not be the
    # same as our leader. Gating with 'snap.refresh.set' ensures layer-snap
    # has finished and we are free to set our config to the leader's timer.
    timer = leader_get('snapd_refresh') or ''  # None will cause error
    log('setting snapd_refresh timer to: {}'.format(timer))
    snap.set_refresh_timer(timer)


@when('tls_client.ca.saved', 'tls_client.server.key.saved',
      'tls_client.server.certificate.saved',
      'tls_client.client.certificate.saved')
@when_not('etcd.ssl.placed')
def tls_state_control():
    ''' This state represents all the complexity of handling the TLS certs.
        instead of stacking decorators, this state condenses it into a single
        state we can gate on before progressing with secure setup. Also handles
        ensuring users of the system can access the TLS certificates'''

    bag = EtcdDatabag()
    if not os.path.isdir(bag.etcd_conf_dir):
        hookenv.log('Waiting for etcd conf creation.')
        return
    cmd = ['chown', '-R', 'root:ubuntu', bag.etcd_conf_dir]
    check_call(cmd)
    set_state('etcd.ssl.placed')


@when('etcd.ssl.placed')
@when_any('tls_client.ca.written',
          'tls_client.server.certificate.written',
          'tls_client.client.certificate.written')
@when_not('upgrade.series.in-progress')
def tls_update():
    ''' Handle changes to the TLS data by ensuring that the service is
        restarted.
    '''
    # ensure config is updated with new certs and service restarted
    bag = EtcdDatabag()
    render_config(bag)
    host.service_restart(bag.etcd_daemon)

    # ensure that certs are re-echoed to the db relations
    remove_state('etcd.ssl.placed')
    remove_state('tls_client.ca.written')
    remove_state('tls_client.server.certificate.written')
    remove_state('tls_client.client.certificate.written')


@when('snap.installed.etcd')
@when_not('etcd.ssl.exported')
def render_default_user_ssl_exports():
    ''' Add secure credentials to default user environment configs,
    transparently adding TLS '''
    opts = layer.options('tls-client')

    ca_path = opts['ca_certificate_path']
    client_crt = opts['client_certificate_path']
    client_key = opts['client_key_path']

    etcd_ver = etcd_version()
    if etcd_ver == 'n/a':
        hookenv.log('Unable to determine version format for etcd SSL config',
                    level=hookenv.ERROR)
        return
    major, minor, _ = etcd_ver.split('.')

    if int(major) >= 3 and int(minor) >= 3:
        evars = [
            'export ETCDCTL_KEY={}\n'.format(client_key),
            'export ETCDCTL_CERT={}\n'.format(client_crt),
            'export ETCDCTL_CACERT={}\n'.format(ca_path)
        ]
    else:
        evars = [
            'export ETCDCTL_KEY_FILE={}\n'.format(client_key),
            'export ETCDCTL_CERT_FILE={}\n'.format(client_crt),
            'export ETCDCTL_CA_FILE={}\n'.format(ca_path)
        ]

    with open('/home/ubuntu/.bash_aliases', 'w') as fp:
        fp.writelines(evars)
    with open('/root/.bash_aliases', 'w') as fp:
        fp.writelines(evars)

    set_state('etcd.ssl.exported')


def force_rejoin():
    """Wipe local data and rejoin new cluster formed by leader unit

    This action is required if leader unit performed snapshot restore. All
    other members must remove their local data and previous cluster
    identities and join newly formed, restored, cluster.
    """
    log('Wiping local storage and rejoining cluster')
    conf = EtcdDatabag()
    host.service_stop(conf.etcd_daemon)
    clear_flag('etcd.registered')
    etcd_data = os.path.join(conf.storage_path(), 'member')
    if os.path.exists(etcd_data):
        shutil.rmtree(etcd_data)
    for _ in range(11):
        # We need randomized back-off timer because only one unit can be
        # joining at the same time
        time.sleep(random.randint(1, 10))
        register_node_with_leader(None)
        if is_flag_set('etcd.registered'):
            log('Successfully rejoined the cluster')
            break


@when('leadership.changed.force_rejoin')
@when_not('leadership.is_leader')
def force_rejoin_requested():
    force_rejoin()
    check_cluster_health()


@hook('cluster-relation-broken')
def perform_self_unregistration(cluster=None):
    ''' Attempt self removal during unit teardown. '''
    etcdctl = EtcdCtl()
    leader_address = leader_get('leader_address')
    unit_name = os.getenv('JUJU_UNIT_NAME').replace('/', '')
    members = etcdctl.member_list()
    # Self Unregistration
    etcdctl.unregister(members[unit_name]['unit_id'], leader_address)


@hook('data-storage-attached')
def format_and_mount_storage():
    ''' This allows users to request persistent volumes from the cloud provider
    for the purposes of disaster recovery. '''
    set_state('data.volume.attached')
    # Query juju for the information about the block storage
    device_info = storage_get()
    block = device_info['location']
    bag = EtcdDatabag()
    bag.cluster = leader_get('cluster')
    # the databag has behavior that keeps the path updated.
    # Reference the default path from layer_options.
    etcd_opts = layer.options('etcd')
    # Split the tail of the path to mount the volume 1 level before
    # the data directory.
    tail = os.path.split(bag.etcd_data_dir)[0]

    if volume_is_mounted(block):
        hookenv.log('Device is already attached to the system.')
        hookenv.log('Refusing to take action against {}'.format(block))
        return

    # Format the device in non-interactive mode
    cmd = ['mkfs.ext4', device_info['location'], '-F']
    hookenv.log('Creating filesystem on {}'.format(device_info['location']))
    hookenv.log('With command: {}'.format(' '.join(cmd)))
    check_call(cmd)

    # halt etcd to perform the data-store migration
    host.service_stop(bag.etcd_daemon)

    os.makedirs(tail, exist_ok=True)
    mount_volume(block, tail)
    # handle first run during early-attach storage, pre-config-changed hook.
    os.makedirs(bag.etcd_data_dir, exist_ok=True)

    # Only attempt migration if directory exists
    if os.path.isdir(etcd_opts['etcd_data_dir']):
        migrate_path = "{}/".format(etcd_opts['etcd_data_dir'])
        output_path = "{}/".format(bag.etcd_data_dir)
        cmd = ['rsync', '-azp', migrate_path, output_path]

        hookenv.log('Detected existing data, migrating to new location.')
        hookenv.log('With command: {}'.format(' '.join(cmd)))

        check_call(cmd)

    with open('/etc/fstab', 'r') as fp:
        contents = fp.readlines()

    found = 0
    # scan fstab for the device
    for line in contents:
        if block in line:
            found = found + 1

    # if device not in fstab, append so it persists through reboots
    if not found > 0:
        append = "{0} {1} ext4 defaults 0 0".format(block, tail)  # noqa
        with open('/etc/fstab', 'a') as fp:
            fp.writelines([append])

    # Finally re-render the configuration and resume operation
    render_config(bag)
    host.service_restart(bag.etcd_daemon)


def read_tls_cert(cert):
    ''' Reads the contents of the layer-configured certificate path indicated
    by cert. Returns the utf-8 decoded contents of the file '''
    # Load the layer options for configured paths
    opts = layer.options('tls-client')

    # Retain a dict of the certificate paths
    cert_paths = {'ca.crt': opts['ca_certificate_path'],
                  'server.crt': opts['server_certificate_path'],
                  'server.key': opts['server_key_path'],
                  'client.crt': opts['client_certificate_path'],
                  'client.key': opts['client_key_path']}

    # If requesting a cert we dont know about, raise a ValueError
    if cert not in cert_paths.keys():
        raise ValueError('No known certificate {}'.format(cert))

    # Read the contents of the cert and return it in utf-8 encoded text
    with open(cert_paths[cert], 'r') as fp:
        data = fp.read()
        return data


@when('nrpe-external-master.available')
@when_not('nrpe-external-master.initial-config')
def initial_nrpe_config(nagios=None):
    set_state('nrpe-external-master.initial-config')
    update_nrpe_config(nagios)


@when_any('config.changed.nagios_context',
          'config.changed.nagios_servicegroups')
def force_update_nrpe_config():
    remove_state('etcd.nrpe.configured')


@when('etcd.installed')
@when('nrpe-external-master.available')
@when_not('etcd.nrpe.configured')
def update_nrpe_config(unused=None):
    # List of systemd services that will be checked
    services = ('snap.etcd.etcd',)

    # The current nrpe-external-master interface doesn't handle a lot of logic,
    # use the charm-helpers code for now.
    hostname = nrpe.get_nagios_hostname()
    current_unit = nrpe.get_nagios_unit_name()
    nrpe_setup = nrpe.NRPE(hostname=hostname, primary=False)
    # add our first check, to alert on service failure
    nrpe.add_init_service_checks(nrpe_setup, services, current_unit)

    # add the cron job to populate the cache for our second check
    # (we cache the output of 'etcdctl alarm list' to minimise overhead)
    with open("templates/check_etcd-alarms.cron") as fp:
        write_file(
            path="/etc/cron.d/check_etcd-alarms",
            content=fp.read().encode(),
            owner="root",
            perms=0o644,
        )

    # create an empty output file for the above
    write_file(
        path="/var/lib/nagios/etcd-alarm-list.txt",
        content="",
        owner="root",
        perms=0o644,
    )

    # install the NRPE script for the above
    with open("templates/check_etcd-alarms.py") as fp:
        write_file(
            path="/usr/lib/nagios/plugins/check_etcd-alarms.py",
            content=fp.read().encode(),
            owner="root",
            perms=0o755,
        )

    # define our second check, to alert on etcd alarm status
    nrpe_setup.add_check(
        "etcd-alarms",
        "Verify etcd has no raised alarms",
        "/usr/lib/nagios/plugins/check_etcd-alarms.py",
    )

    nrpe_setup.write()
    set_state('etcd.nrpe.configured')


@when_not('nrpe-external-master.available')
@when('nrpe-external-master.initial-config')
def remove_nrpe_config(nagios=None):
    remove_state('nrpe-external-master.initial-config')

    # List of systemd services for which the checks will be removed
    services = ('snap.etcd.etcd',)

    # The current nrpe-external-master interface doesn't handle a lot of logic,
    # use the charm-helpers code for now.
    hostname = nrpe.get_nagios_hostname()
    nrpe_setup = nrpe.NRPE(hostname=hostname, primary=False)

    for service in services:
        nrpe_setup.remove_check(shortname=service)


@when('endpoint.prometheus.joined',
      'leadership.is_leader',
      'certificates.ca.available')
def register_prometheus_jobs():
    # This function is not guarded with `when_not("prometheus.configured")`
    # to account for possible changes of etcd units IP adresses and for when
    # etcd units are added/removed. Repeated calls to `prometheus.register_job()`
    # have no effect unless job_data changes.
    log('Registering Prometheus metrics collection.')
    prometheus = endpoint_from_flag('endpoint.prometheus.joined')
    cluster = endpoint_from_flag('cluster.joined')

    peer_ips = cluster.get_db_ingress_addresses() if cluster else []
    peer_ips.append(get_ingress_address('db'))
    targets = ["{}:{}".format(ip, config('port')) for ip in peer_ips]
    log('Configuring Prometheus scrape targets: {}'.format(targets), DEBUG)
    prometheus.register_job(job_name='etcd',
                            job_data={
                                'scheme': 'https',
                                'static_configs': [
                                    {'targets': targets},
                                ]
                            })
    set_flag('prometheus.configured')


@when(
    "prometheus.configured",
    "endpoint.grafana.joined",
    "leadership.is_leader"
)
@when_not("grafana.configured")
def register_grafana_dashboard():
    log("Configuring grafana dashboard", level=hookenv.INFO)
    grafana = endpoint_from_flag("endpoint.grafana.joined")
    prometheus = endpoint_from_flag('endpoint.prometheus.joined')

    if not prometheus:
        log(
            "Prometheus relation not available. Skipping Grafana"
            " configuration.", hookenv.WARNING)
        return

    if len(prometheus.relations) > 1:
        log(
            "Multiple prometheus relations detected. Default Grafana dashboard"
            " will configure only with one of them as datasource.",
            hookenv.WARNING)

    datasource = prometheus.relations[0].application_name
    dashboard = render_grafana_dashboard(datasource)

    log("Rendered Grafana dashboard:\n{}".format(json.dumps(dashboard)),
        level=hookenv.DEBUG)
    grafana.register_dashboard(name=GRAFANA_DASHBOARD_NAME,
                               dashboard=dashboard)
    log('Grafana dashboard "{}" registered.'.format(GRAFANA_DASHBOARD_NAME))
    set_flag("grafana.configured")


def volume_is_mounted(volume):
    ''' Takes a hardware path and returns true/false if it is mounted '''
    cmd = ['df', '-t', 'ext4']
    out = check_output(cmd).decode('utf-8')
    return volume in out


def mount_volume(volume, location):
    ''' Takes a device path and mounts it to location '''
    cmd = ['mount', volume, location]
    hookenv.log("Mounting {0} to {1}".format(volume, location))
    check_call(cmd)


def unmount_path(location):
    ''' Unmounts a mounted volume at path '''
    cmd = ['umount', location]
    hookenv.log("Unmounting {0}".format(location))
    check_call(cmd)


def close_open_ports():
    ''' Close the previous port and open the port from configuration. '''
    configuration = hookenv.config()
    previous_port = configuration.previous('port')
    port = configuration.get('port')
    if previous_port is not None and previous_port != port:
        log('The port changed; closing {0} opening {1}'.format(previous_port,
            port))
        close_port(previous_port)
        open_port(port)


def install(src, tgt):
    ''' This method wraps the bash "install" command '''
    return check_call(split('install {} {}'.format(src, tgt)))


def render_config(bag=None):
    ''' Render the etcd configuration template for the given version '''
    if not bag:
        bag = EtcdDatabag()

    move_etcd_data_to_standard_location()

    v2_conf_path = "{}/etcd.conf".format(bag.etcd_conf_dir)
    v3_conf_path = "{}/etcd.conf.yml".format(bag.etcd_conf_dir)

    # probe for 2.x compatibility
    if etcd_version().startswith('2.'):
        render('etcd2.conf', v2_conf_path, bag.__dict__, owner='root',
               group='root')
    # default to 3.x template behavior
    else:
        render('etcd3.conf', v3_conf_path, bag.__dict__, owner='root',
               group='root')
        if os.path.exists(v2_conf_path):
            # v3 will fail if the v2 config is left in place
            os.remove(v2_conf_path)
    # Close the previous client port and open the new one.
    close_open_ports()
    remove_state('etcd.rerender-config')


def etcd_version():
    ''' This method surfaces the version from etcdctl '''
    raw_output = None
    try:
        # try v3
        raw_output = check_output(
            ['/snap/bin/etcd.etcdctl', 'version'],
            env={'ETCDCTL_API': '3'}
        ).decode('utf-8').strip()
        if "No help topic for 'version'" in raw_output:
            # handle v2
            raw_output = check_output(
                ['/snap/bin/etcd.etcdctl', '--version']
            ).decode('utf-8').strip()
        for line in raw_output.splitlines():
            if 'etcdctl version' in line:
                # "etcdctl version: 3.0.17" or "etcdctl version 2.3.8"
                version = line.split()[-1]
                return version
        hookenv.log('Unable to find etcd version: {}'.format(raw_output),
                    level=hookenv.ERROR)
        return 'n/a'
    except (ValueError, CalledProcessError):
        hookenv.log('Failed to get etcd version:\n'
                    '{}'.format(traceback.format_exc()), level=hookenv.ERROR)
        return 'n/a'


def move_etcd_data_to_standard_location():
    ''' Moves etcd data to the standard location if it's not already located
    there. This is necessary when generating new etcd config after etcd has
    been upgraded from version 2.3 to 3.x.
    '''
    bag = EtcdDatabag()
    conf_path = bag.etcd_conf_dir + '/etcd.conf.yml'
    if not os.path.exists(conf_path):
        return
    with open(conf_path) as f:
        conf = yaml.safe_load(f)
    data_dir = conf['data-dir']
    desired_data_dir = bag.etcd_data_dir
    if data_dir != desired_data_dir:
        log('Moving etcd data from %s to %s' % (data_dir, desired_data_dir))
        host.service_stop('snap.etcd.etcd')
        for filename in os.listdir(data_dir):
            os.rename(
                data_dir + '/' + filename,
                desired_data_dir + '/' + filename
            )
        os.rmdir(data_dir)
        conf['data-dir'] = desired_data_dir
        with open(conf_path, 'w') as f:
            yaml.dump(conf, f)
        host.service_start('snap.etcd.etcd')
