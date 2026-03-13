========
mongokit
========

|PyPI version| |PyPI pyversions| |PyPI license|

mongokit, a.k.a. ``mkit``, is a utility to quickly set up complex MongoDB test environments 
on a local machine, including replica sets and sharded clusters. It was 
originally part of the now deprecated `mtools 
<https://github.com/rueckstiess/mtools>`__ collection; however, is now available
as a standalone tool.

.. figure:: https://raw.githubusercontent.com/mongodb/mongokit/develop/mkit.png
   :alt: mtools box

For more information, see the `mkit documentation
<https://mongodb.github.io/mongokit>`__.

Requirements and Installation Instructions
------------------------------------------

`mkit` is written in Python. The tools are currently tested with Python 3.8,
3.9, 3.10, and 3.11.

mkit requires `pymongo`, `psutil` and `packaging` dependencies. See the 
`installation instructions <https://mongodb.github.io/mongokit/install.html>`__
for more information.

mkit is only tested with
`actively supported <https://www.mongodb.com/support-policy/lifecycles>`__
(non End-of-Life) versions of the MongoDB server. As of November 2025,
that includes MongoDB 7.0 or newer.

Using mongokit
--------------
After installing mongokit, you can run it from the command line by typing
``mkit``. For a list of available commands, run:

.. code-block:: bash

   mkit --help

For detailed usage instructions, see the `mkit documentation
<https://mongodb.github.io/mongokit/mkit.html>`__.

Recent Changes
--------------

See `the changelog <https://mongodb.github.io/mongokit/changelog.html>`__
for a list of changes from previous versions of mkit/mkit.

Contribute to mongokit
----------------------

If you'd like to contribute to mongokit, please read the `contributor page
<https://mongodb.github.io/mongokit/contributing.html>`__ for instructions.

Disclaimer
----------

This software is not supported by `MongoDB, Inc. <https://www.mongodb.com>`__
under any of their commercial support subscriptions or otherwise. Any usage of
mongokit is at your own risk. Bug reports, feature requests and questions can be
posted in the `Issues
<https://github.com/mongodb/mongokit/issues?state=open>`__ section on GitHub.

.. |PyPI version| image:: https://img.shields.io/pypi/v/mongokit.svg
   :target: https://pypi.python.org/pypi/mongokit/
.. |PyPI pyversions| image:: https://img.shields.io/pypi/pyversions/mongokit.svg
   :target: https://pypi.python.org/pypi/mongokit/
.. |PyPI license| image:: https://img.shields.io/pypi/l/mongokit.svg
   :target: https://pypi.python.org/pypi/mongokit/
