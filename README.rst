========
mongorun
========

|PyPI version| |PyPI pyversions| |PyPI license|

``mrun``, is a utility to quickly set up complex MongoDB test environments 
on a local machine, including replica sets and sharded clusters. It was 
originally part of the now deprecated `mtools 
<https://github.com/rueckstiess/mtools>`__ collection; however, is now available
as a standalone tool.

.. figure:: https://raw.githubusercontent.com/mongodb/mongorun/develop/mrun.png
   :alt: mtools box

For more information, see the `mongorun documentation
<https://mongodb.github.io/mongorun>`__.

Requirements and Installation Instructions
------------------------------------------

`mrun` is written in Python. The tools are currently tested with Python 3.8,
3.9, 3.10, and 3.11.

mongorun requires `pymongo`, `psutil` and `packaging` dependencies. See the 
`installation instructions <https://mongodb.github.io/mongorun/install.html>`__
for more information.

mongorun is only tested with
`actively supported <https://www.mongodb.com/support-policy/lifecycles>`__
(non End-of-Life) versions of the MongoDB server. As of November 2025,
that includes MongoDB 7.0 or newer.

Using mongorun
--------------
After installing mongorun, you can run it from the command line by typing
``mrun``. For a list of available commands, run:

.. code-block:: bash

   mrun --help

For detailed usage instructions, see the `mongorun documentation
<https://mongodb.github.io/mongorun/mrun.html>`__.

Recent Changes
--------------

See `the changelog <https://mongodb.github.io/mongorun/changelog.html>`__
for a list of changes from previous versions of mlaunch/mongorun.

Contribute to mongrun
---------------------

If you'd like to contribute to mongorun, please read the `contributor page
<https://mongodb.github.io/mongorun/contributing.html>`__ for instructions.

Disclaimer
----------

This software is not supported by `MongoDB, Inc. <https://www.mongodb.com>`__
under any of their commercial support subscriptions or otherwise. Any usage of
mongorun is at your own risk. Bug reports, feature requests and questions can be
posted in the `Issues
<https://github.com/mongodb/mongorun/issues?state=open>`__ section on GitHub.

.. |PyPI version| image:: https://img.shields.io/pypi/v/mongorun.svg
   :target: https://pypi.python.org/pypi/mongorun/
.. |PyPI pyversions| image:: https://img.shields.io/pypi/pyversions/mongorun.svg
   :target: https://pypi.python.org/pypi/mongorun/
.. |PyPI license| image:: https://img.shields.io/pypi/l/mongorun.svg
   :target: https://pypi.python.org/pypi/mongorun/
