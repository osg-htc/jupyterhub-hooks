KubeSpawner Hooks for OSG's JupyterHub Instance
===============================================

This Python package provides KubeSpawner_ hooks for customizing a user's
server options based on the groups that they belong to.

These hooks are specific to OSG's COmanage infrastructure and the OSPool.

.. _KubeSpawner: https://github.com/jupyterhub/kubespawner


Installation
------------

Use ``pip`` to install this package into the same Python environment as
JupyterHub::

    python3 -m pip install git+https://github.com/osg-htc/jupyterhub-hooks.git@<ref>

Replace ``<ref>`` with a tag_ or any other Git ref into this repository.

.. _tag: https://github.com/osg-htc/jupyterhub-hooks/tags


Configuration
-------------

Via JupyterHub's configuration, configure ``KubeSpawner`` to use the hooks::

    from osg.jupyterhub import kubespawner_hooks
    c.KubeSpawner.auth_state_hook = kubespawner_hooks.auth_state_hook
    c.KubeSpawner.options_form = kubespawner_hooks.options_form
    c.KubeSpawner.modify_pod_hook = kubespawner_hooks.modify_pod_hook

The hooks are configured via a YAML file::

    /etc/osg/kubespawner_hooks_config.yaml

The structure is determined by the class ``Configuration`` in `<osg/jupyterhub/kubespawner_hooks.py>`_.


Development
-----------

This project uses Poetry_ to manage its dependencies:

1. Install Poetry.

2. Run ``poetry update`` to install dependencies.

The dependencies replicate the environment provided by a particular version
of Z2JH_'s ``k8s-hub`` image. (Refer to the comments in `<pyproject.toml>`_.)

This project uses pre-commit_ to ensure commits meet minimum requirements:

1. Run ``poetry run pre-commit install`` to install the Git hooks.

The `<Makefile>`_ provides ``reformat`` and ``lint`` targets for running
various standard tools (``isort``, ``black``, ``pylint``, etc.).

.. _Poetry: https://python-poetry.org/
.. _pre-commit: https://pre-commit.com/
.. _Z2JH: https://z2jh.jupyter.org/
