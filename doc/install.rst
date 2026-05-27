============
Installation
============

`mrun` is written in Python and tested against Python 3.10+.

mongorun is only tested with
`actively supported <https://www.mongodb.com/support-policy/lifecycles>`__
(non End-of-Life) versions of the MongoDB server. As of May 2026,
that includes MongoDB 7.0 or newer.

Prerequisites
~~~~~~~~~~~~~

Python
   You need to have Python 3.10+ installed in order to use mtools. Earlier 
   versions of Python are not tested and not guaranteed to function.

   To check your Python version, run ``python --version`` on the command line.

Installation with pip3
~~~~~~~~~~~~~~~~~~~~~~

The easiest way to install mongorun is via ``pip3``. From the command line, run:

.. code-block:: bash

   pip3 install mongorun

You need to have Python 3.10 or newer installed. ``pip3`` should be included as
part of the default install for supported versions of Python 3.

Depending on your user rights, ``pip3`` may complain about not having
permissions to install into the system directory.

In that case, you either need to add ``sudo`` in front of the ``pip3`` command
to install into a system directory, or append ``--user`` to install into your
home directory.

Installation from source
~~~~~~~~~~~~~~~~~~~~~~~~

If ``pip3`` is not available and you want to install mongorun from source, you can
get the source code by cloning the `mongorun github repository
<https://github.com/mongodb/mongorun>`__:

.. code-block:: bash

   git clone git://github.com/mongodb/mongorun.git

Or download the tarball from `PyPI <https://pypi.python.org/pypi/mongorun>`__ and
extract it with:

.. code-block:: bash

   tar xzvf mongorun-<version>.tar.gz

Then ``cd`` into the mongorun directory and run:

.. code-block:: bash

   sudo python setup.py install

This will install mongorun into your Python's site-packages folder, create links
to the scripts and set everything up. You should now be able to use all the
scripts directly from the command line.

.. _dependencies:

Dependencies
~~~~~~~~~~~~

The full list of requirements (some of which are already included in the Python
standard library) can be found in the `requirements.txt
<https://github.com/mongodb/mongorun/blob/develop/requirements.txt>`__ file.

psutil
------

mongorun uses ``psutil`` to manage starting, stopping, and finding MongoDB
processes.

pymongo
-------

`pymongo <https://www.mongodb.com/docs/drivers/pymongo/#installation>`__
is MongoDB's official Python driver. ``mrun`` uses this to configure
and query local MongoDB deployments.
