osg-jupyter
===========

This Python package provides `KubeSpawner`_ hooks that can be used to
customize single-user notebook servers in non-trivial ways. It is intended
to support `OSG`_'s customizations for `OSPool`_ users.

.. _KubeSpawner: https://jupyterhub-kubespawner.readthedocs.io/en/latest/
.. _OSG: https://osg-htc.org/
.. _OSPool: https://osg-htc.org/services/open_science_pool.html


Installation
------------

Use ``pip`` to install this package directly from this repository::

    python3 -m pip install git+https://github.com/brianaydemir/osg-jupyter.git@<ref>

In most cases, replace ``<ref>`` with the `tag for a specific version`_::

    python3 -m pip install git+https://github.com/brianaydemir/osg-jupyter.git@1.0.0

.. _tag for a specific version: https://github.com/brianaydemir/osg-jupyter/tags


KubeSpawner Configuration
-------------------------

Some documentation can be found in `<osg/jupyter/kubespawner.py>`_.

The default location for the configuration file is ``/etc/osg/jupyterhub_kubespawner.yaml``.

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

    # The `ospool-patches` key is similar to `patches` except that it
    # applies only when the logged in user is also an OSPool user.

    ospool-patches:

    - path: notebook/security_context
      op: set
      value:
        run_as_user: "{user.uid}"
        run_as_group: "{user.gid}"
        _: V1SecurityContext
