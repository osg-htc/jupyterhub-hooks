# Minimal JupyterLab with support for sssd and HTCondor.
#
# The intent is that /var/lib/sss/pipes will be shared with some other
# container that is running an sssd daemon. The notebook container should
# not include configuration for sssd.

FROM htcondor/htc-minimal-notebook:latest

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

ENTRYPOINT ["tini", "-g", "--", "/opt/osg/bin/entrypoint.sh"]
