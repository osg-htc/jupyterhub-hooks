KubeSpawner Hooks for OSG's JupyterHub Instance
===============================================

This Python package provides KubeSpawner_ hooks for customizing a user's
server options based on the groups that they belong to.

These hooks derive a user's group membership from their OIDC userinfo
and are tailored to the OSPool.

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
    c.KubeSpawner.pre_spawn_hook = kubespawner_hooks.pre_spawn_hook

The hooks are configured via a YAML file::

    /etc/osg/kubespawner_hooks_config.yaml

The structure is determined by the class ``Configuration`` in `<osg/jupyterhub/kubespawner_hooks.py>`_.


Virtual groups
--------------

A server option can be gated on group membership by listing group names
under a ``ProfileList``'s or ``KubespawnerOverride``'s ``groups`` key.
The hooks gate on the user's *effective* groups: the union of their
OIDC groups (from the OIDC userinfo) and their native JupyterHub
group memberships.

A "virtual group" is simply a native JupyterHub group.
By convention, virtual groups granted by the code-entry service (below)
are prefixed with ``code-``.
Because a virtual group is an ordinary group name, gating a server option
on one requires no schema change -- an administrator lists a ``code-...``
group name wherever they would list an OIDC group.

For example, to show a tutorial's server option only to users who have
entered its access code::

    server_lists:
      - groups: ["code-tutorial-2026"]
        servers:
          - display_name: "Tutorial 2026 environment"
            kubespawner_override: { ... }
      - groups: []
        servers:
          - display_name: "Default"
            kubespawner_override: { ... }

Option visibility is presentation only; it is not an enforcement
boundary, because ``user_options`` can be POSTed directly to the spawn
API.
Use ``require_code`` (below) to enforce a requirement.


Requiring a code to spawn
-------------------------

The ``require_code`` section makes ``pre_spawn_hook`` block a spawn for
users whose username matches a regular expression unless they belong to
at least one of the listed groups (real or virtual)::

    require_code:
      - when_username_matches: '^(ext|guest)_.*$'
        satisfied_by_groups: ["code-verified"]
        message: >-
          Your account must be verified with an access code before
          launching a server.

The username is matched with ``re.fullmatch``, so the pattern must
match the entire username.
Omitting ``require_code`` (or setting it to ``[]``) disables this check.

When a rule applies to a user but is not satisfied, the effect is
twofold: ``options_form`` hides every server option and shows the rule's
``message`` in place of the form, and ``pre_spawn_hook`` blocks the spawn
as a backstop (also showing ``message``) in case the form is bypassed.
So ``message`` is the text a gated user sees in both places.


Code-entry service
-------------------

The code-entry service (``osg.jupyterhub_code_service``) lets an
authenticated user submit a shared code and, on success, adds the user
to one or more native JupyterHub groups.
It is the only component that knows the code secrets; the hooks only
read group membership.

Run it as a JupyterHub-managed service::

    import os

    # The Hub derives the OAuth redirect URI and injects
    # JUPYTERHUB_SERVICE_URL from this "url".
    #
    # A managed service runs *in the hub pod*, so it must be reachable at
    # this host. Use the hub pod's own IP, not 127.0.0.1: the proxy runs
    # in a separate pod and cannot reach the hub pod's loopback, so a
    # loopback "url" yields a 503. The pod IP is routable both in-pod (the
    # Hub's startup health-check) and cross-pod (the proxy). Inject it via
    # the downward API (see below). Any free port works.
    pod_ip = os.environ["POD_IP"]

    c.JupyterHub.services = [
        {
            "name": "enter-code",
            "command": ["python", "-m", "osg.jupyterhub_code_service"],
            "url": f"http://{pod_ip}:10101",
            "oauth_no_confirm": True,
        },
    ]

    c.JupyterHub.load_roles = [
        {
            "name": "enter-code-role",
            "services": ["enter-code"],
            # Use ["admin:groups"] only if CODE_SERVICE_CREATE_MISSING_GROUPS
            # is enabled; otherwise the "groups" scope is sufficient and the
            # target groups must already exist.
            "scopes": ["groups"],
        },
        {
            # Let users reach the service. Without this scope the service is
            # hidden from the Services menu and navigating to it returns 403.
            # Defining a role named "user" extends the default user role, so
            # this grants every user access; keep "self" or the default
            # self-access scope is lost. To limit access instead, drop this
            # entry and grant "access:services!service=enter-code" to a
            # specific group via its own role.
            "name": "user",
            "scopes": ["self", "access:services!service=enter-code"],
        },
    ]

Inject the hub pod's IP with the Kubernetes downward API so that
``POD_IP`` is available to the Hub configuration above.
With the Zero to JupyterHub chart::

    hub:
      extraEnv:
        POD_IP:
          valueFrom:
            fieldRef:
              fieldPath: status.podIP

If NetworkPolicies are enabled, allow ingress to the hub pod on the
service port (``10101`` above) from the proxy pod; the default hub policy
opens only the Hub API port, which would otherwise leave the route
unreachable and produce a 503.

The service also calls the Hub API, both to authenticate requests and to
manage group membership.
It runs inside the hub pod but reaches the API through
``JUPYTERHUB_API_URL``, which Z2JH sets to the hub *Service*
(``http://<release>-hub:8081/hub/api``), so the request hairpins out to
the ClusterIP and back to the same pod's API port.
The default hub policy allows ingress on ``8081`` from the proxy and
singleuser pods, not from the hub pod itself, so under a deny-by-default
policy this self-connection is dropped and every request to the service
times out (for example, ``Error connecting to
http://<release>-hub:8081/hub/api: Timeout while connecting``).
Allow ingress to the hub pod on the Hub API port (``8081``) from the hub
pod itself::

    hub:
      networkPolicy:
        ingress:
          - from:
              - podSelector:
                  matchLabels:
                    app.kubernetes.io/component: hub
            ports:
              - port: 8081
                protocol: TCP

Match the label keys to your rendered hub pod.
Alternatively, avoid the hairpin by overriding the service's
``JUPYTERHUB_API_URL`` to loopback (``http://127.0.0.1:8081/hub/api``)
via the service's ``environment`` key; the Hub binds all interfaces, so
loopback resolves in-pod.
This mirrors the ``url`` guidance above: the service's inbound ``url``
must not be loopback because the proxy reaches it cross-pod, but its
outbound Hub API calls can be, because they stay within the pod.
Because ``JUPYTERHUB_API_URL`` also drives the OAuth layer, verify that
login still works if you take this route.

The service appears in the "Services" dropdown of the JupyterHub home /
control-panel page (the Hub's top navigation, not the JupyterLab or
notebook header). It is listed only when it has a ``url`` (above),
``display`` is left at its default of ``True``, and the user holds the
``access:services`` scope granted above.

The Hub injects the ``JUPYTERHUB_SERVICE_*`` and ``JUPYTERHUB_API_*``
environment variables.
The service reads this additional configuration from the environment:

``CODE_SERVICE_CONFIG`` (default ``/etc/osg/code_service_config.yaml``)
    Path to the codes file; mount it from a Secret.

``CODE_SERVICE_GROUP_PREFIX`` (default ``code-``)
    Required prefix for grantable group names.
    A grant that does not match the prefix is dropped.

``CODE_SERVICE_CREATE_MISSING_GROUPS`` (default ``false``)
    When ``true``, create a target group if the Hub returns 404.
    This requires the ``admin:groups`` scope.

``CODE_SERVICE_BURST`` (default ``5``)
    Number of attempts a user may make in quick succession (the token
    bucket's capacity), so a few rapid typos are tolerated.

``CODE_SERVICE_REFILL_SECONDS`` (default ``10``)
    Seconds to regain one attempt once the burst is spent.

Together the two defaults let a user make up to five quick attempts and
then throttle them to a sustained one attempt every ten seconds.

The codes file is a YAML document whose structure is determined by the
class ``CodeServiceConfig`` in `<osg/jupyterhub_code_service/config.py>`_.
It holds secrets and must not be committed; mount it from a Secret::

    codes:
      - id: tutorial-2026-spring
        secret: "<the shared code>"
        grants: ["code-tutorial-2026"]
        expires: "2026-05-01T00:00:00Z"
      - id: verified-external-batch-a
        secret: "<the shared code>"
        grants: ["code-verified"]

The codes file is re-read shortly after each submission, mirroring the
KubeSpawner hooks, so editing it (adding a code, changing grants, or
fixing a code) takes effect without restarting the service.
To keep responses timely and to avoid re-reading storage on every
request, each service caches the parsed file for up to about fifteen
seconds and reloads it in the background; a slow or hung mount is served
from the last-good config rather than stalling the request.
So an edit can take up to that long to take effect, on top of the
delay for a mounted Secret to update on disk.
A code's ``expires`` only stops *new* grants; it does not revoke an
existing group membership.
This service assumes a single replica.
The rate limiter's state is per-process, so a second replica would not
share it; substitute a shared store to scale.
The cookie-signing secret is likewise generated per process, so cookies
signed by one replica fail validation on another, breaking login; scaling
requires pinning to one replica or sharing a stable secret.


Caveat: ``Authenticator.manage_groups``
---------------------------------------

Virtual groups are native JupyterHub groups.
If the deployment's authenticator runs with ``manage_groups = True``,
JupyterHub resets a user's group membership from the identity provider on
every login, which would wipe code-granted groups.
This design requires ``Authenticator.manage_groups`` to be ``False`` (the
default) so that native group membership is authoritative and durable.
The OSG hooks derive OIDC groups from the OIDC userinfo at spawn time,
independently of native JupyterHub groups, so this should hold -- but it
must be verified per deployment.


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
