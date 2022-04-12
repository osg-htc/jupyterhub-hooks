## Minimal JupyterLab with support for sssd and HTCondor.

FROM htcondor/htc-minimal-notebook:latest

## Stop the HTC notebook from starting its local/private pool.

ENV _condor_SCHEDD_HOST ""

# In order to accommodate the sharing of sssd's "pipes" directory, we
# create the sssd user and group with explicit IDs rather than allowing
# packages to select arbitrary IDs.

USER root

RUN groupadd -r -g 996 sssd \
    && useradd -r -g sssd -u 996 -d / -s /usr/sbin/nologin -c "System user for sssd" sssd \
    #
    && apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y sssd \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY opt /opt/

USER jovyan

## Start the notebook server.

CMD /opt/osg/bin/start-notebook.sh
