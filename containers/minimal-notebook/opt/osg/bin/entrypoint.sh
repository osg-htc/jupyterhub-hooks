#!/bin/sh
# Prepare the host before starting the notebook server.

set -eu

## Wait for sssd to create its sockets when not running as "jovyan".

_sssd_socket=/var/lib/sss/pipes/nss

if [ "$(id -u)" != "1000" ]; then
  until test -e "${_sssd_socket}"; do
    printf 'Waiting for %s to be created\n' "${_sssd_socket}"
    sleep 2
  done
fi

## Write the user's HTCondor IDTOKEN to a file.

_idtokens_dir="$HOME/.condor/tokens.d"

if [ -n "${_osg_HTCONDOR_IDTOKEN:-}" ]; then
  if [ ! -d "${_idtokens_dir}" ]; then
    mkdir -p "${_idtokens_dir}"
    chmod u=rwx,go= "${_idtokens_dir}"
  fi
  printf '%s' "${_osg_HTCONDOR_IDTOKEN}" > "${_idtokens_dir}/$(hostname)"
fi

exec "$@"
