import os
import errno
import logging

import cephfs
import orchestrator

from .exception import VolumeException

log = logging.getLogger(__name__)

def create_pool(mgr, pool_name, pg_num):
    # create the given pool
    command = {'prefix': 'osd pool create', 'pool': pool_name, 'pg_num': pg_num}
    return mgr.mon_command(command)

def remove_pool(mgr, pool_name):
    command = {'prefix': 'osd pool rm', 'pool': pool_name, 'pool2': pool_name,
               'yes_i_really_really_mean_it': True}
    return mgr.mon_command(command)

def create_filesystem(mgr, fs_name, metadata_pool, data_pool):
    command = {'prefix': 'fs new', 'fs_name': fs_name, 'metadata': metadata_pool,
               'data': data_pool}
    return mgr.mon_command(command)

def remove_filesystem(mgr, fs_name):
    command = {'prefix': 'fs fail', 'fs_name': fs_name}
    r, outb, outs = mgr.mon_command(command)
    if r != 0:
        return r, outb, outs

    command = {'prefix': 'fs rm', 'fs_name': fs_name, 'yes_i_really_mean_it': True}
    return mgr.mon_command(command)

def create_mds(mgr, fs_name):
    spec = orchestrator.StatelessServiceSpec()
    spec.name = fs_name
    try:
        completion = mgr.add_stateless_service("mds", spec)
        mgr._orchestrator_wait([completion])
        orchestrator.raise_if_exception(completion)
    except (ImportError, orchestrator.OrchestratorError):
        return 0, "", "Volume created successfully (no MDS daemons created)"
    except Exception as e:
        # Don't let detailed orchestrator exceptions (python backtraces)
        # bubble out to the user
        log.exception("Failed to create MDS daemons")
        return -errno.EINVAL, "", str(e)
    return 0, "", ""

def volume_exists(mgr, fs_name):
    fs_map = mgr.get('fs_map')
    for fs in fs_map['filesystems']:
        if fs['mdsmap']['fs_name'] == fs_name:
            return True
    return False

def listdir(fs, dirpath):
    """
    Get the directory names (only dirs) for a given path
    """
    dirs = []
    try:
        with fs.opendir(dirpath) as dir_handle:
            d = fs.readdir(dir_handle)
            while d:
                if (d.d_name not in (b".", b"..")) and d.is_dir():
                    dirs.append(d.d_name)
                d = fs.readdir(dir_handle)
    except cephfs.Error as e:
        raise VolumeException(-e.args[0], e.args[1])
    return dirs

def list_one_entry_at_a_time(fs, dirpath):
    """
    Get a directory entry (one entry a time)
    """
    try:
        with fs.opendir(dirpath) as dir_handle:
            d = fs.readdir(dir_handle)
            while d:
                if d.d_name not in (b".", b".."):
                    yield d
                d = fs.readdir(dir_handle)
    except cephfs.Error as e:
        raise VolumeException(-e.args[0], e.args[1])

def copy_file(fs, src, dst, mode):
    """
    Copy a regular file from @src to @dst. @dst is overwritten if it exists.
    """
    src_fd = dst_fd = None
    try:
        src_fd = fs.open(src, os.O_RDONLY);
        dst_fd = fs.open(dst, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, mode)
    except cephfs.Error as e:
        if src_fd is not None:
            fs.close(src_fd)
        if dst_fd is not None:
            fs.close(dst_fd)
        raise VolumeException(-e.args[0], e.args[1])

    IO_SIZE = 8 * 1024 * 1024
    try:
        while True:
            data = fs.read(src_fd, -1, IO_SIZE)
            if not len(data):
                break
            written = 0
            while written < len(data):
                written += fs.write(dst_fd, data[written:], -1)
        fs.fsync(dst_fd, 0)
    except cephfs.Error as e:
        raise VolumeException(-e.args[0], e.args[1])
    finally:
        fs.close(src_fd)
        fs.close(dst_fd)

def get_ancestor_xattr(fs, path, attr):
    """
    Helper for reading layout information: if this xattr is missing
    on the requested path, keep checking parents until we find it.
    """
    try:
        return fs.getxattr(path, attr).decode('utf-8')
    except cephfs.NoData as e:
        if path == "/":
            raise VolumeException(-e.args[0]. e.args[1])
        else:
            return get_ancestor_xattr(fs, os.path.split(path)[0], attr)
