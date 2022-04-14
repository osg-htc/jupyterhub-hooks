# JupyterHub with support for OSG.
#
# For use with Zero to JupyterHub for Kubernetes (https://z2jh.jupyter.org/).

FROM jupyterhub/k8s-hub:0.11.1

USER root

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    #
    && python3 -m pip install --no-cache-dir osg-jupyter==0.0.1

USER jovyan
