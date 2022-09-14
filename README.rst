osg-jupyter
===========

This Python package provides `KubeSpawner`_ hooks that can be used to
customize single-user notebook servers in non-trivial ways.

.. _KubeSpawner: https://jupyterhub-kubespawner.readthedocs.io/en/latest/


Installation
------------

Use ``pip`` to install this package directly from this repository::

    pip install git+https://github.com/brianaydemir/osg-jupyter.git@<ref>

In most cases, replace ``<ref>`` with the `tag for a specific version`_::

    pip install git+https://github.com/brianaydemir/osg-jupyter.git@1.0.0

.. _tag for a specific version: https://github.com/brianaydemir/osg-jupyter/tags


KubeSpawner Configuration
-------------------------

Documentation can be found in `<osg/jupyter/kubespawner.py>`_.

Sample configuration file::

    # The `exceptions` key is a list of notebook images for which the
    # Kubernetes Pod should *not* be patched. Images should be specified
    # using Python regular expressions.

    exceptions:

    - image: jupyterhub/k8s-singleuser-sample

    # The `patches` key is a list of patch operations. Note that paths
    # and keys are based on the Kubernetes Python API.

    patches:

    - path: pod/spec/volumes
      op: extend
      value:
      - name: shared-data
        nfs:
          server: nfs.example.com
          path: /data
          _: V1NFSVolumeSource
        _: V1Volume

    - path: notebook/volume_mounts
      op: extend
      value:
      - name: shared-data
        mount_path: /data
        _: V1VolumeMount
