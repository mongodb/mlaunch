=======
mlaunch
=======

|PyPI version| |PyPI pyversions| |PyPI license|

``mlaunch``, is a utility to quickly set up complex MongoDB test environments 
on a local machine, including replica sets and sharded clusters. It was 
originally part of the now deprecated `mtools 
<https://github.com/rueckstiess/mtools>`__ collection; however, is now available
as a standalone tool.

.. figure:: https://raw.githubusercontent.com/mongodb/mlaunch/develop/mlaunch.png
   :alt: mtools box

For more information, see the `mlaunch documentation
<https://mongodb.github.io/mlaunch>`__.

Requirements and Installation Instructions
------------------------------------------

`mlaunch` is written in Python. The tools are currently tested with Python 3.8,
3.9, 3.10, and 3.11.

mlaunch requires `pymongo`, `psutil` and `packaging` dependencies. See the 
`installation instructions <https://mongodb.github.io/mlaunch/install.html>`__
for more information.

mlaunch is only tested with
`actively supported <https://www.mongodb.com/support-policy/lifecycles>`__
(non End-of-Life) versions of the MongoDB server. As of November 2025,
that includes MongoDB 7.0 or newer.

Using mlaunch
-------------
After installing mlaunch, you can run it from the command line by typing
``mlaunch``. For a list of available commands, run:

.. code-block:: bash

   mlaunch --help

For detailed usage instructions, see the `mlaunch documentation
<https://mongodb.github.io/mlaunch/mlaunch.html>`__.

Recent Changes
--------------

See `the changelog <https://mongodb.github.io/mlaunch/changelog.html>`__
for a list of changes from previous versions of mlaunch/mlaunch.

Contribute to mlaunch
---------------------

If you'd like to contribute to mlaunch, please read the `contributor page
<https://mongodb.github.io/mlaunch/contributing.html>`__ for instructions.

Disclaimer
----------

This software is not supported by `MongoDB, Inc. <https://www.mongodb.com>`__
under any of their commercial support subscriptions or otherwise. Any usage of
mlaunch is at your own risk. Bug reports, feature requests and questions can be
posted in the `Issues
<https://github.com/mongodb/mlaunch/issues?state=open>`__ section on GitHub.

.. |PyPI version| image:: https://img.shields.io/pypi/v/mtools.svg
   :target: https://pypi.python.org/pypi/mtools/
.. |PyPI pyversions| image:: https://img.shields.io/pypi/pyversions/mtools.svg
   :target: https://pypi.python.org/pypi/mtools/
.. |PyPI license| image:: https://img.shields.io/pypi/l/mtools.svg
   :target: https://pypi.python.org/pypi/mtools/
