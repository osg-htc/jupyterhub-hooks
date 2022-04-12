#!/bin/sh
# Wait for sssd to create its sockets before starting JupyterLab.

set -eu

_target=/var/lib/sss/pipes/nss

until test -e "${_target}"
do
  printf 'Waiting for %s (does not exist)\n' "${_target}"
  sleep 2
done

exec jupyter-labhub
