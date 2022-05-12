# JupyterHub with support for OSG.
#
# For use with Zero to JupyterHub for Kubernetes (https://z2jh.jupyter.org/).

FROM jupyterhub/k8s-hub:0.11.1

USER root

RUN apt-get update \
    && apt-get upgrade -y \
    #
    && apt-get install -y git \
    && git clone \
         https://github.com/brianaydemir/oauthenticator.git \
         /build/oauth \
    && python3 -m pip install -U --no-cache-dir /build/oauth \
    && rm -rf /build/ \
    && apt-get remove -y git \
    #
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir osg-jupyter==0.5.0-a1

USER jovyan
