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

The default location for the configuration file is ``/etc/osg/jupyterhub_kubespawner.yaml``.

Its structure is defined by the class ``Configuration`` in `<osg/jupyter/kubespawner.py>`_.
