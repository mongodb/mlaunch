.. _mrun:

========
mongorun
========

This tool lets you quickly spin up and monitor MongoDB environments on your
local machine. It supports various configurations of stand-alone servers,
replica sets and sharded clusters. Individual nodes or groups of nodes can
easily be stopped and started again.

In addition to all the listed parameters of **mrun** below, you can pass in
any arbitrary options that a ``mongos`` or ``mongod`` binary would understand,
and **mrun** will pass them on to the correct binary. This includes the
``-f`` option to read further options from a MongoDB configuration file.


Usage
~~~~~

.. code-block:: bash

   mrun [-h] [--version] [--no-progressbar] [--monitor] [--all]
        [--dir DIR] [--monitor-username USER]
        [--monitor-password PASSWORD] [--monitor-auth-db DB]
   mrun [-h] [--version] [--no-progressbar]
           {init,start,stop,restart,list,kill} ...


General Parameters
~~~~~~~~~~~~~~~~~~

The following parameters work with all commands.

Help
----
``-h, --help``
   shows the help text and exits.

Version
-------
``--version``
   shows the version number and exits.

Monitor
-------
``--monitor``
   opens a live terminal monitor for running **mrun** managed ``mongod`` and
   ``mongos`` processes from the selected data directory. The monitor shows
   CPU usage, memory usage, network activity, disk consumption, and a
   selectable live log tail with severity colors and local filtering. The left
   side stacks CPU, memory, network, and disk metrics; the right side shows the
   log tail or a configurable top-N active ``currentOp`` activity view. CPU,
   memory, network, disk, and currentOp rows include a ``ROLE`` column for
   primary/secondary state. Role values use muted, non-bold semantic colors
   that do not compete with pane headers. Pane borders stay neutral, while pane
   titles and table headers are bold and color-coded by pane. CPU, memory,
   network, disk, and currentOp rows share an ANSI-aware table formatter for
   stable column alignment. The CPU pane defaults to normalized process CPU,
   where 100% means all logical CPUs on the host. Press ``C`` while the CPU
   pane is focused to toggle back to raw ``psutil`` process CPU, which can
   exceed 100% on multi-core hosts. If no
   **mrun**
   managed MongoDB server processes are running, **mrun** will print a message
   and exit.

``--all``
   used with ``--monitor`` to include all local ``mongod`` and ``mongos``
   processes instead of only the processes listed in ``.mrun_startup``.

``--monitor-username USER``, ``--monitor-password PASSWORD``,
``--monitor-auth-db DB``
   optional credential overrides used only by ``--monitor`` samplers and the
   ``mongosh`` handoff. By default, monitor mode loads credentials from
   ``.mrun_startup`` when an authenticated **mrun** deployment created an
   initial user.

   Monitor controls:

   -  ``q`` or ``Ctrl+C`` quits.
   -  In log view, ``r`` reselects log files. The selector accepts list
      indexes or displayed ports and re-prompts when a token does not match a
      visible entry.
   -  In currentOp view, ``r`` opens the currentOp source selector.
   -  ``a`` toggles the process scope between **mrun** managed processes and
      all detected local MongoDB processes, then prompts for log selection
      again. The footer reports the active scope as ``scope:mrun`` or
      ``scope:all``.
   -  ``Tab`` and ``Shift+Tab`` move focus across CPU, memory, network, disk,
      and logs panes.
   -  ``z`` toggles a full-screen view for the focused pane.
   -  In the CPU pane, ``j``/``k`` or up/down arrows select a MongoDB process.
   -  In the CPU pane, ``C`` toggles the CPU column between normalized and raw
      process CPU.
   -  In the CPU pane, ``t`` toggles between the default process CPU list and
      a thread view for the selected process. Thread view is never shown by
      default.
   -  ``o`` toggles the right activity pane between the log tail and the active
      ``currentOp`` entries across visible MongoDB processes. The default limit
      is 10 entries.
   -  ``O`` toggles formatted and raw ``currentOp`` documents while the
      currentOp activity view is active. Raw mode displays BSON-safe text
      derived from ``db.currentOp()`` output.
   -  ``L`` opens a currentOp top-N prompt while currentOp is active. Values
      lower than 1 are rejected, and large values are capped at 500 entries.
   -  ``r`` opens a currentOp source selector while currentOp is active. The
      selector accepts process indexes, ports, ``primary``, ``secondary``,
      ``all``, or Enter for all visible processes. Selecting ``primary`` makes
      currentOp sampling run only against the primary node.
   -  ``n`` opens a currentOp namespace selector while currentOp is active.
      The selector lists active namespaces and also accepts a typed namespace.
   -  ``c`` clears the currentOp namespace filter while currentOp is active,
      or clears the log filter while the logs pane is active.
   -  ``p`` prettifies the highlighted currentOp document as syntax-colored
      JSON while currentOp is active. Press ``p`` again to return to the
      currentOp list.
   -  ``y`` yanks the highlighted currentOp while currentOp is active. The
      copied text follows the active currentOp mode: formatted row, raw JSON,
      or Pretty JSON.
   -  In the logs pane, ``j``/``k`` or up/down arrows move the highlighted log
      cursor. The text stays still while the cursor moves inside the visible
      window and scrolls only when the cursor reaches the visible edge.
   -  ``g`` jumps to the newest log line and resumes live-follow.
   -  ``/`` opens a log filter prompt. Press Enter to apply the typed filter,
      or Esc to cancel.
   -  ``c`` clears the active log filter from the logs pane.
   -  Filters support free text, fuzzy matching, ``slowop``, and structured
      fields such as ``cmd:find``, ``component:COMMAND``, ``severity:E``,
      ``port:27017``, and ``msg:"Slow query"``.
   -  ``p`` prettifies the highlighted line as syntax-colored JSON, pauses
      live-follow, and expands the log view. Press ``p`` again to return to
      the raw log line.
   -  ``y`` yanks the highlighted log line to the terminal clipboard when
      supported.
   -  Space pauses or resumes log streaming in the log tail. While paused, the
      current log buffer stays frozen; resuming catches up from the same file
      offset. While currentOp is active, Space pauses or resumes currentOp
      sampling and keeps the last sampled currentOp rows visible.
   -  ``s`` cycles the refresh interval through 1, 5, and 10 seconds.
   -  ``M`` launches an interactive ``mongosh`` administration shell, when the
      executable is available. The monitor offers primary, selected-node,
      seed-list, first-visible-node, and custom URI targets, reuses stored
      auth/TLS metadata, and passes ``--password`` without the password value
      so ``mongosh`` prompts securely.
   -  Log lines are color coded by severity: fatal uses red inverse, errors
      use red, warnings use yellow, info uses muted teal, and debug uses dim
      gray. Selection uses inverse video so it remains visible without hiding
      the severity color. After yanking, that line is highlighted green.
   -  Moving away from the newest log line pauses live-follow. Moving back to
      the newest line resumes live-follow.
   -  Footer key names are highlighted separately from their action labels so
      pane-specific actions are easier to distinguish while the dashboard is
      running.

   Pretty JSON colors are selected from the terminal background when
   ``COLORFGBG`` is available. Use ``MRUN_MONITOR_THEME=dark`` or
   ``MRUN_MONITOR_THEME=light`` to override automatic theme detection.

   When authentication metadata exists but monitor credentials are unavailable,
   role and currentOp views show ``Password Required``. Stored credentials from
   ``.mrun_startup`` or explicit monitor credential overrides are used when
   available. The ``M`` shell handoff also refuses to launch for auth-enabled
   deployments when credentials are required but unavailable.

Verbosity
---------
``--verbose``
   This will print additional information, depending on each of the commands.

Data directory
--------------
``--dir DIR``
   This parameter changes the directory where **mrun** stores its data and
   log files. By default, the directory is the local directory ``./data``,
   below the current working directory.


Commands
~~~~~~~~

``mrun`` uses different commands to initialize, stop, start and list test
environments. The general syntax is:

.. code-block:: bash

   mrun <command> [--parameters ...]

where ``<command>`` is one of the following choices:

-  ``init``: creates an initial environment and starts all nodes
-  ``stop``: stops some or all nodes in the current environment
-  ``start``: starts some or all nodes in the current environment
-  ``list``: shows a list of the current environment
-  ``kill``: sends a kill (or other) signal to the nodes in the current
   environment

For a given environment (specified by its data directory with the ``--dir``
argument, the default is ``./data``), the ``init`` command only needs to be
called once. ``mrun`` stores the configuration in a config file within the
data directory, called ``.mrun_startup``. With this file, mongorun remembers
the configuration and can ``start`` and ``stop`` nodes when required.

-----

init
----

This command initializes and starts MongoDB stand-alone instances, replica
sets, or sharded clusters. It only needs to be called once for each environment
(specified by its data directory with the ``--dir`` argument, the default is
``./data``).

Usage
^^^^^

.. code-block:: bash

   mrun init [-h] (--single | --replicaset) [--nodes NUM] [--arbiter]
                [--name NAME] [--priority] [--sharded N [N ...]]
                [--config NUM] [--csrs] [--mongos NUM] [--verbose]
                [--port PORT] [--binarypath PATH] [--dir DIR]
                [--hostname HOSTNAME] [--auth] [--username USERNAME]
                [--password PASSWORD] [--auth-db DB]
                [--auth-roles [ROLE [ROLE ...]]] [--auth-role-docs]
                [--no-initial-user] [--sslCAFile SSLCAFILE]
                [--sslCRLFile SSLCRLFILE] [--sslAllowInvalidHostnames]
                [--sslAllowInvalidCertificates]
                [--sslMode {disabled,allowSSL,preferSSL,requireSSL}]
                [--sslPEMKeyFile SSLPEMKEYFILE]
                [--sslPEMKeyPassword SSLPEMKEYPASSWORD]
                [--sslClusterFile SSLCLUSTERFILE]
                [--sslClusterPassword SSLCLUSTERPASSWORD]
                [--sslDisabledProtocols SSLDISABLEDPROTOCOLS]
                [--sslAllowConnectionsWithoutCertificates] [--sslFIPSMode]
                [--sslClientCertificate SSLCLIENTCERTIFICATE]
                [--sslClientPEMKeyFile SSLCLIENTPEMKEYFILE]
                [--sslClientPEMKeyPassword SSLCLIENTPEMKEYPASSWORD]

For convenience and backwards compatibility, the ``init`` command is the
default command and can be omitted.

Required Parameters
^^^^^^^^^^^^^^^^^^^
The ``init`` command requires **exactly one** of the following two parameters
to run: ``--single`` or ``--replicaset``. They are mutually exclusive and one
must be specified for each ``mrun init`` execution.

``--single``
   This parameter will create a single stand-alone node. If ``--sharded`` is
   also specified, this parameter will create each shard as a single
   stand-alone node.

   For example, to start a single ``mongod`` instance on port 27017:

   .. code-block:: bash

      mrun --single

``--replicaset``
   This parameter will create a replica set rather than a single node. Other
   :ref:`mrun-repl-params` apply and can modify the properties of the
   replica set to launch. If ``--sharded`` is also specified, this parameter
   will create one such replica sets for each shard.

   For example, to start a replica set with (by default) 3 nodes on ports
   27017, 27018, 27019:

   .. code-block:: bash

      mrun --replicaset

.. _mrun-repl-params:

Replica Set Parameters
^^^^^^^^^^^^^^^^^^^^^^

The following parameters change how a replica set is set up. These parameters
require that you picked the ``--replicaset`` option from the required
parameters.

``--nodes N``
   Specifies the number of data-bearing nodes (arbiters not included) for this
   replica set. The default value is 3.

   For example:

   .. code-block:: bash

      mrun --replicaset --nodes 5

   This command starts 5 mongod instances and configures them to one replica
   set.

``--arbiter``
   If this parameter is present, an additional arbiter is added to the replica
   set. Currently, **mrun** only supports adding one arbiter. Additional
   arbiters can be started and added to the replica set manually.

   For example:

   .. code-block:: bash

      mrun --replicaset --nodes 2 --arbiter

   This command starts 2 data-bearing mongod instances and adds one arbiter to
   the replica set, for a total of 3 voting nodes.

``--name NAME``
   This option lets you modify the name of the replica set. This will change
   both the name and the sub-directory of the ``dbpath``. This option is only
   allowed for a single replica sets and will not work in sharded setups, where
   replica set names are equivalent to the shard names. The default name is
   ``replset``.

   For example:

   .. code-block:: bash

      mrun --replicaset --name "my_rs_1"

   This command will create a replica set with the name ``my_rs_1`` and will
   also store the dbpath and log files under ``./data/my_rs_1``.

Sharding Parameters
^^^^^^^^^^^^^^^^^^^

The following parameters influence the setup of a sharded environment. Each
shard will be a copy of the previously specified setup, be it a single instance
or a replica set.

``--sharded S [S ...]``
   If this parameter is provided, sharding is enabled and **mrun** will
   create the specified number of shards and add the shards together to a
   sharded cluster. The parameter can work in two ways: Either by specifying a
   single number, which is the number of shards, or by specifying a list of
   shard names.

   For example:

   .. code-block:: bash

      mrun --single --sharded 3

   This command will create an environment of 3 shards, each consisting of a
   single stand-alone node. The shard names are ``shard0001``, ``shard0002``,
   ``shard0003``. It will also create 1 config server and 1 mongos per default.

   For example:

   .. code-block:: bash

      mrun --replicaset --sharded tic tac toe

   This command will create 3 shards, named ``tic``, ``tac`` and ``toe``. Each
   shard will consist of a replica set of (per default) 3 nodes. It will also
   create 1 config server and 1 mongos per default.

``--config N``
   This parameter determines, how many config servers are launched in a sharded
   environment. The default number is 1. The only valid options for ``N`` are 1
   or 3.

``--csrs``
   This parameter has ``mrun`` use `Config Servers as a Replica Set (CSRS)
   <https://docs.mongodb.com/manual/core/sharded-cluster-config-servers/#replica-set-config-servers>`__
   rather than the older Sync Cluster Connection Config (SCCC).

   The CSRS deployment option is supported by MongoDB 3.2+, and as of MongoDB
   3.4 is the default (and only) supported option.

   If you are using MongoDB 3.4 and greater, ``mrun`` will use CSRS by
   default.

   *Changed in version 1.2.3*

   CSRS config servers will no longer include incompatible settings, such as:

   -  ``--storageEngine`` -- CSRS config servers will always use WiredTiger.
   -  ``--arbiter`` -- CSRS config servers cannot have any arbiter.

``--mongos N``
   This parameter determines, how many ``mongos`` instances are launched in a
   sharded environment. The default number is 1. With this setting, the default
   can be changed to ``N`` mongos instances.

Authentication Parameters
^^^^^^^^^^^^^^^^^^^^^^^^^

``--auth``
   This parameter enables authentication on your setup. It will transparently
   work with single instances (that require ``--auth``) as well as replica sets
   and sharded environments (that require ``--keyFile``). There is no need to
   additionally specify a keyfile, a random keyfile will be generated for you.

   A username and password will also be set up, either on the mongos for
   sharded environments, or on the primary node for replica sets or on a single
   node.

``--username``
   This parameter changes the default username ``user`` to the specified user.

``--password``
   This parameter changes the default password ``password`` to the specified
   password.

   .. note::

      The default password is chosen deliberately to be easy to remember or
      guess. ``mrun`` is meant for testing and issue reproduction, not for
      production use. Even a strong password will not guarantee security with
      mongorun-generated environments, because the username and password are
      included in the ``data/.mrun_startup`` file in clear text.

``--auth-db``
   This parameter changes the default database, from ``admin``, in which the
   user will be created.

   .. note::

      If you change the database, it may not be possible for ``mrun`` to
      execute certain commands due to missing privileges. This may lead to
      unexpected behavior for some ``mrun`` operations, like for example
      ``mrun stop``, which uses the internal ``shutdown`` command. If this
      is the case, use ``mrun kill`` instead.

``--auth-roles``
   This parameter changes the initial default roles that the user will receive.
   The default roles are ``dbAdminAnyDatabase``, ``readWriteAnyDatabase``,
   ``userAdminAnyDatabase`` and ``clusterAdmin``. You can provide different
   roles with this parameter, separated by spaces.

   .. note::

      If you change the default roles, it may not be possible for ``mrun``
      to execute certain commands due to missing privileges. This may lead to
      unexpected behavior for some ``mrun`` operations, like for example
      ``mrun stop``, which uses the internal ``shutdown`` command. If this
      is the case, use ``mrun kill`` instead.

   For example:

   .. code-block:: bash

      mrun --sharded 2 --single --auth --username thomas --password my_s3cr3t_p4ssw0rd

   This command would start a sharded cluster with 2 single shards, 1 config
   server, 1 mongos, and create the user ``thomas`` with password
   ``my_s3cr3t_p4ssw0rd``. It will use the default roles and place the user in
   the ``admin`` database. ``mrun`` will

``--auth-role-docs``
   Use with ``--auth-roles`` to interpret roles specified as JSON documents.

``--no-initial-user``
   Do not create an initial user if auth is enabled.

Optional Parameters
^^^^^^^^^^^^^^^^^^^

``--port PORT``
   Uses ``PORT`` as the start port number for the first instance, and increases
   the number by one for each additional instance (mongod/mongos). By default,
   the start port value is MongoDB's standard port 27017. Use this parameter to
   start several setups in parallel on different port ranges.

   For example:

   .. code-block:: bash

      mrun --replicaset --nodes 3 --port 30000

   This command would start a replica set of 3 nodes using ports 30000, 30001
   and 30002.

``--binarypath PATH``
   Will set the path where **mrun** looks for the binaries of ``mongod`` and
   ``mongos`` to the provided ``PATH``. By default, the $PATH environment
   variable is used to determine which binary is started. You can use this
   option to overwrite the default setting. This is useful for example if you
   compile your own source code and want mongorun to use the compiled version.

   For example:

   .. code-block:: bash

      mrun --single --binarypath ./build/bin

   This command will look for the ``mongod`` binary in ``./build/bin/mongod``
   instead of the default location.

TLS/SSL options
^^^^^^^^^^^^^^^
``--sslCAFile SSLCAFILE``
   Certificate Authority file for TLS/SSL.

``--sslCRLFile SSLCRLFILE``
   Certificate Revocation List file for TLS/SSL.

``--sslAllowInvalidHostnames``
   Allow client and server certificates to provide non-matching hostnames.

``--sslAllowInvalidCertificates``
   Allow client or server connections with invalid
   certificates.

Server TLS/SSL options
^^^^^^^^^^^^^^^^^^^^^^

``--sslMode {disabled,allowSSL,preferSSL,requireSSL}``
   Set the TLS/SSL operation mode.

``--sslPEMKeyFile SSLPEMKEYFILE``
   PEM file for TLS/SSL.

``--sslPEMKeyPassword SSLPEMKEYPASSWORD``
   PEM file password.

``--sslClusterFile SSLCLUSTERFILE``
   Key file for internal TLS/SSL authentication.

``--sslClusterPassword SSLCLUSTERPASSWORD``
   Internal authentication key file password.

``--sslDisabledProtocols SSLDISABLEDPROTOCOLS``
   Comma separated list of TLS protocols to disable [TLS1_0,TLS1_1,TLS1_2].

``--sslAllowConnectionsWithoutCertificates``
   Allow client to connect without presenting a certificate.

``--sslFIPSMode``
   Activate FIPS 140-2 mode.

Client TLS/SSL options
^^^^^^^^^^^^^^^^^^^^^^

``--sslClientCertificate SSLCLIENTCERTIFICATE``
   Client certificate file for TLS/SSL.

``--sslClientPEMKeyFile SSLCLIENTPEMKEYFILE``
   Client PEM file for TLS/SSL.

``--sslClientPEMKeyPassword SSLCLIENTPEMKEYPASSWORD``
   Client PEM file password.

-----

.. _mrun-kill:

kill
----

The ``kill`` command stops some or all running nodes in the current
environment, depending on the specified tags, by sending the processes the
``SIGTERM`` (15) signal.

If no tags are specified, ``mrun kill`` will kill all nodes. If one or more
tags are specified, ``mrun kill`` will only kill the nodes that have all of
the given tags (set intersection). This works even if there is no ``admin``
user with the ``clusterAdmin`` role.

Instead of the ``SIGTERM`` signal, other signals can be specified with the
``--signal`` parameter. (not available on Windows)

Usage
^^^^^

.. code-block:: bash

   mrun kill [TAG [TAG ...]] [--signal S] [--dir DIR] [--verbose]


Tag Parameters
^^^^^^^^^^^^^^

The following tags are used with mrun, although not all tags are present in
every environment:

-  ``all``: all nodes in the environment.
-  ``running``: all currently running nodes.
-  ``down``: all currently down nodes.
-  ``mongos``: all mongos processes carry this tag.
-  ``mongod``: all mongod processes (including arbiters and config servers).
-  ``config``: all config servers
-  ``shard``: this tag is only used to identify a specific shard number (see
   below).
-  ``<shard name>``: for sharded environments, each member of a shard carries
   the shard name as a tag, e.g. "shard-a".
-  ``primary``: all running primary nodes.
-  ``secondary``: all running secondary nodes.
-  ``arbiter``: all arbiters.
-  ``<port number>``: each node carries its port number as a tag.

If a single tag is specified for the ``kill`` command, the nodes matching this
tag will be killed. If multiple tags are specified, only the nodes matching
**all tags** are killed. Each tag will narrow down the set of matches further.

For example:

.. code-block:: bash

   mrun kill

This command kills all running nodes in the current environment.

For example:

.. code-block:: bash

   mrun kill mongos

This command kills all running mongos processes in the current environment.

For example:

.. code-block:: bash

   mrun kill shard-a secondary

This command kills all running secondary nodes of the shard called 'shard-a' in
the current environment.

For example:

.. code-block:: bash

   mrun kill config primary

This command would not kill any nodes, because there is no node with both tags
``config`` and ``primary``.

For example:

.. code-block:: bash

   mrun kill 27017

This command would kill the node running on port 27017.

In addition, some tags can be combined with a succeeding number. These tags
are: ``mongos``, ``shard``, ``config``, ``secondary``.

For example:

.. code-block:: bash

   mrun kill shard 1

This command kills all members of shard 1 in the current _sharded_ environment.

For example:

.. code-block:: bash

   mrun kill shard 2 primary

This command kills the primary of the second shard in the current _sharded_
environment.

For example:

.. code-block:: bash

   mrun kill secondary 1

This command kills the first secondary node of all shards if the environment is
_sharded_. If the environment is a _replicaset_, it only applies to the first
secondary.

For example:

.. code-block:: bash

   mrun kill

This command sends signal ``SIGTERM`` (15) to all running processes in the
current environment.

For example:

.. code-block:: bash

   mrun kill --signal SIGUSR1

This command sends signal ``SIGUSR1`` (30) to all running processes in the
current environment, which in MongoDB causes a log rotation.

-----

.. _mrun-start:

start
-----

The ``start`` command starts some or all nodes that are currently down in the
current environment, depending on the specified tags. If no tags are specified,
``mrun start`` will start all nodes. If one or more tags are specified,
``mrun start`` will only start the nodes that have all of the given tags
(set intersection).

Usage
^^^^^

.. code-block:: bash

   mrun start [TAG [TAG ...]] [--dir DIR] [--verbose]

Tag Parameters
^^^^^^^^^^^^^^

The following tags are used with mrun, although not all tags are present in
every environment:

-  ``all``: all nodes in the environment.
-  ``running``: all currently running nodes.
-  ``down``: all currently down nodes.
-  ``mongos``: all mongos processes carry this tag.
-  ``mongod``: all mongod processes (including arbiters and config servers).
-  ``config``: all config servers
-  ``shard``: this tag is only used to identify a specific shard number (see
   below).
-  ``<shard name>``: for sharded environments, each member of a shard carries
   the shard name as a tag, e.g. "shard-a".
-  ``arbiter``: all arbiters.
-  ``<port number>``: each node carries its port number as a tag.

Different to the ``stop`` command, there tags for ``primary`` and ``secondary``
are not available for the ``start`` command. This is because the replica set
state of a running node is undetermined.

For examples, see :ref:`mrun-stop`.

-----

.. _mrun-stop:

stop
----

The ``stop`` command stops some or all running nodes in the current
environment, depending on the specified tags, by sending the ``shutdown``
command to the mongod or mongos instance.

If no tags are specified, ``mrun stop`` will stop all nodes. If one or more
tags are specified, ``mrun stop`` will only stop the nodes that have all of
the given tags (set intersection).

In authenticated environments, the ``stop`` command requires a user in the
``admin`` database with the ``clusterAdmin`` role. Otherwise the ``stop``
command will not succeed. In that case, you can use the ``kill`` command
instead.

*Changed in version 1.2.3*

As of version 1.2.3, the ``stop`` command is an alias for the ``kill`` command.

For examples, see :ref:`mrun-kill`.

Usage
^^^^^

.. code-block:: bash

   mrun stop [TAG [TAG ...]] [--dir DIR] [--verbose]

Tag Parameters
^^^^^^^^^^^^^^

The tags for the ``stop`` command are the same as for :ref:`mrun-kill`.

-----

restart
-------

The ``restart`` command stops, then restarts some or all nodes in the current
environment, depending on the specified tags. It is added for convenience and
behaves like a ``stop`` and ``start`` command in succession. If no tags are
specified, ``mrun restart`` will restart all nodes. If one or more tags are
specified, ``mrun restart`` will only restart the nodes that have all of the
given tags (set intersection).


Usage
^^^^^

.. code-block:: bash

   mrun restart [TAG [TAG ...]] [--dir DIR] [--verbose]


Tag Parameters
^^^^^^^^^^^^^^

See :ref:`mrun-start` and :ref:`mrun-stop`.

-----

list
----

The ``list`` command shows an overview of all nodes in the current environment,
as well as their status (running/down) and port. With the optional
``--verbose`` flag, the list command also shows all tags for each node.


Usage
^^^^^

.. code-block:: bash

   mrun list [-h] [--dir DIR] [--json] [--tags] [--startup] [--verbose]

For example:

.. code-block:: bash

   mrun list

   PROCESS          STATUS     PORT

   mongos           running    27017
   mongos           running    27018

   config server    running    27025
   config server    running    27026
   config server    down       27027

   shard01
       primary      running    27019
       secondary    running    27020
       arbiter      running    27021

   shard02
       mongod       down       27022
       mongod       down       27023
       mongod       down       27024

This command displays a list of all nodes, their status and port number. In
this case, the environment was started with:

.. code-block:: bash

   mrun --sharded 2 --replicaset --nodes 2 --arbiter --config 3 --mongos 2

Optional Parameters
^^^^^^^^^^^^^^^^^^^

``--json``
   This option outputs the results in JSON format, which may be useful for
   integration with other tools or scripts.

``--tags``
   This option additionally shows a column with all the tags that the instance
   can be addressed with. Tags can be used to target certain instances for
   ``start``, ``stop``, ``kill``, etc. commands.

   For example:

   .. code-block:: bash

      mrun list --tags

      PROCESS      STATUS     PORT     TAGS

      primary      running    27017    27017, all, mongod, primary, running
      secondary    running    27018    27018, all, mongod, running, secondary
      mongod       down       27019    27019, all, down, mongod

   This command displays a list of all nodes, their status and port number, and
   in addition, their tags. In this case, the environment was started with:

   .. code-block:: bash

      mrun --replicaset

``--startup``
   This option additionally shows a column with the startup strings that was
   used to run the given instance. This is useful to if an instance needs to be
   started manually.

   For example:

   .. code-block:: bash

      mrun list --startup

      PROCESS      PORT     STATUS     PID     STARTUP COMMAND

      secondary    27017    running    4264    mongod --replSet replset --dbpath /tmp/data/replset/rs1/db --logpath /tmp/data/replset/rs1/mongod.log --port 27017 --logappend --fork -vv
      mongod       27018    running    4267    mongod --replSet replset --dbpath /tmp/data/replset/rs2/db --logpath /tmp/data/replset/rs2/mongod.log --port 27018 --logappend --fork -vv
      mongod       27019    running    4270    mongod --replSet replset --dbpath /tmp/data/replset/rs3/db --logpath /tmp/data/replset/rs3/mongod.log --port 27019 --logappend --fork -vv

   This command displays a list of all nodes, their status and port number, and
   in addition, their startup commands.

Disclaimer
~~~~~~~~~~

This software is not supported by `MongoDB, Inc. <https://www.mongodb.com>`__
under any of their commercial support subscriptions or otherwise. Any usage of
mongorun is at your own risk. Bug reports, feature requests and questions can be
posted in the `Issues
<https://github.com/mongodb/mongorun/issues?state=open>`__ section on GitHub.
