# -*- coding: utf-8 -*-

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
"""This test suite contains tests for bodhi.server.bugs."""

import unittest

import bunch
import mock
import xmlrpclib

from bodhi.server import bugs


class TestBugzilla(unittest.TestCase):
    """This test class contains tests for the Bugzilla class."""
    def test___init__(self):
        """Assert that the __init__ method sets up the Bugzilla object correctly."""
        bz = bugs.Bugzilla()

        self.assertIsNone(bz._bz)

    @mock.patch('bodhi.server.bugs.bugzilla.Bugzilla.__init__', return_value=None)
    def test__connect_with_creds(self, __init__):
        """Test the _connect() method when the config contains credentials."""
        bz = bugs.Bugzilla()
        patch_config = {'bodhi_email': 'bodhi@example.com', 'bodhi_password': 'bodhi_secret',
                        'bz_server': 'https://example.com/bz'}

        with mock.patch.dict('bodhi.server.bugs.config', patch_config):
            bz._connect()

        __init__.assert_called_once_with(url='https://example.com/bz', user='bodhi@example.com',
                                         password='bodhi_secret', cookiefile=None, tokenfile=None)

    @mock.patch('bodhi.server.bugs.bugzilla.Bugzilla.__init__', return_value=None)
    def test__connect_without_creds(self, __init__):
        """Test the _connect() method when the config does not contain credentials."""
        bz = bugs.Bugzilla()
        patch_config = {'bz_server': 'https://example.com/bz'}

        with mock.patch.dict('bodhi.server.bugs.config', patch_config):
            bz._connect()

        __init__.assert_called_once_with(url='https://example.com/bz',
                                         cookiefile=None, tokenfile=None)

    @mock.patch('bodhi.server.bugs.bugzilla.Bugzilla.__init__', return_value=None)
    def test_bz_with__bz_None(self, __init__):
        """
        Assert correct behavior of the bz() method when _bz is None.
        """
        bz = bugs.Bugzilla()
        patch_config = {'bz_server': 'https://example.com/bz'}

        with mock.patch.dict('bodhi.server.bugs.config', patch_config):
            return_value = bz.bz

        __init__.assert_called_once_with(url='https://example.com/bz',
                                         cookiefile=None, tokenfile=None)
        self.assertTrue(return_value is bz._bz)

    @mock.patch('bodhi.server.bugs.Bugzilla._connect')
    def test_bz_with__bz_set(self, _connect):
        """
        Assert correct behavior of the bz() method when _bz is already set.
        """
        bz = bugs.Bugzilla()
        bz._bz = mock.MagicMock()

        return_value = bz.bz

        self.assertTrue(return_value is bz._bz)
        self.assertEqual(_connect.call_count, 0)

    @mock.patch('bodhi.server.bugs.log.exception')
    def test_comment_successful(self, exception):
        """Test the comment() method with a success case."""
        bz = bugs.Bugzilla()
        bz._bz = mock.MagicMock()

        bz.comment(1411188, 'A nice message.')

        bz._bz.getbug.assert_called_once_with(1411188)
        bz._bz.getbug.return_value.addcomment.assert_called_once_with('A nice message.')
        # No exceptions should have been logged
        self.assertEqual(exception.call_count, 0)

    @mock.patch('bodhi.server.bugs.log.exception')
    def test_comment_too_long(self, exception):
        """Assert that the comment() method gets angry if the comment is too long."""
        bz = bugs.Bugzilla()
        bz._bz = mock.MagicMock()
        oh_my = u'All work aind no play makes bowlofeggs a dull… something something… '
        long_comment = oh_my * (65535 / len(oh_my) + 1)

        bz.comment(1411188, long_comment)

        self.assertEqual(bz._bz.getbug.call_count, 0)
        # An exception should have been logged
        exception.assert_called_once_with(
            u'Comment too long for bug #1411188:  {}'.format(long_comment))

    @mock.patch('bodhi.server.bugs.log.exception')
    def test_comment_too_many_attempts(self, exception):
        """Assert that only 5 attempts are made to comment before giving up."""
        bz = bugs.Bugzilla()
        bz._bz = mock.MagicMock()
        bz._bz.getbug.return_value.addcomment.side_effect = xmlrpclib.Fault(
            42, 'Someone turned the microwave on and now the WiFi is down.')

        bz.comment(1411188, 'A nice message.')

        bz._bz.getbug.assert_called_once_with(1411188)
        self.assertEqual([c[1] for c in bz._bz.getbug.return_value.addcomment.mock_calls],
                         [('A nice message.',) for i in range(5)])
        # Five exceptions should have been logged
        self.assertEqual(
            [c[1] for c in exception.mock_calls],
            [(('\nA fault has occurred \nFault code: 42 \nFault string: Someone turned the '
               'microwave on and now the WiFi is down.'),) for i in range(5)])

    @mock.patch('bodhi.server.bugs.log.exception')
    def test_comment_unexpected_exception(self, exception):
        """Test the comment() method with an unexpected Exception."""
        bz = bugs.Bugzilla()
        bz._bz = mock.MagicMock()
        bz._bz.getbug.return_value.addcomment.side_effect = Exception(
            'Ran out of internet fluid, please refill.')

        bz.comment(1411188, 'A nice message.')

        bz._bz.getbug.assert_called_once_with(1411188)
        bz._bz.getbug.return_value.addcomment.assert_called_once_with('A nice message.')
        exception.assert_called_once_with('Unable to add comment to bug #1411188')

    def test_get_url(self):
        """
        Assert correct behavior from the get_url() method.
        """
        bz = bugs.Bugzilla()
        patch_config = {'bz_baseurl': 'https://example.com/bz'}

        with mock.patch.dict('bodhi.server.bugs.config', patch_config):
            self.assertEqual(bz.get_url('42'), 'https://example.com/bz/show_bug.cgi?id=42')

    def test_getbug(self):
        """
        Assert correct behavior on the getbug() method.
        """
        bz = bugs.Bugzilla()
        bz._bz = mock.MagicMock()

        return_value = bz.getbug(1411188)

        self.assertTrue(return_value is bz._bz.getbug.return_value)
        bz._bz.getbug.assert_called_once_with(1411188)

    @mock.patch('bodhi.server.bugs.log.info')
    def test_modified_product_skipped(self, info):
        """Test the modified() method when the bug's product is not in the bz_products config."""
        bz = bugs.Bugzilla()
        bz._bz = mock.MagicMock()
        bz._bz.getbug.return_value.product = 'not fedora!'

        bz.modified(1411188)

        bz._bz.getbug.assert_called_once_with(1411188)
        info.assert_called_once_with("Skipping 'not fedora!' bug")
        self.assertEqual(bz._bz.getbug.return_value.setstatus.call_count, 0)

    @mock.patch('bodhi.server.bugs.log.exception')
    def test_on_qa_failure(self, exception):
        """
        Test the on_qa() method with a failure case.
        """
        bz = bugs.Bugzilla()
        bz._bz = mock.MagicMock()
        bz._bz.getbug.return_value.setstatus.side_effect = Exception(
            'You forgot to pay your oxygen bill. Your air supply will promptly be severed.')

        bz.on_qa(1411188, 'A mean message.')

        bz._bz.getbug.assert_called_once_with(1411188)
        bz._bz.getbug.return_value.setstatus.assert_called_once_with('ON_QA',
                                                                     comment='A mean message.')
        exception.assert_called_once_with('Unable to alter bug #1411188')

    @mock.patch('bodhi.server.bugs.log.exception')
    def test_on_qa_success(self, exception):
        """
        Test the on_qa() method with a success case.
        """
        bz = bugs.Bugzilla()
        bz._bz = mock.MagicMock()

        bz.on_qa(1411188, 'A message.')

        bz._bz.getbug.assert_called_once_with(1411188)
        bz._bz.getbug.return_value.setstatus.assert_called_once_with('ON_QA', comment='A message.')
        self.assertEqual(exception.call_count, 0)


class TestFakeBugTracker(unittest.TestCase):
    """This test class contains tests for the FakeBugTracker class."""
    def test_getbug(self):
        """Ensure correct return value of the getbug() method."""
        bt = bugs.FakeBugTracker()

        b = bt.getbug(1234)

        self.assertTrue(isinstance(b, bunch.Bunch))
        self.assertEqual(b.bug_id, 1234)

    @mock.patch('bodhi.server.bugs.log.debug')
    def test___noop__(self, debug):
        """Assert correct behavior from the __noop__ method."""
        bt = bugs.FakeBugTracker()

        bt.__noop__(1, 2)

        debug.assert_called_once_with('__noop__((1, 2))')


class TestSetBugtracker(unittest.TestCase):
    """
    Test the set_bugtracker() function.
    """
    @mock.patch('bodhi.server.bugs.bugtracker', None)
    @mock.patch.dict('bodhi.server.bugs.config', {'bugtracker': 'bugzilla'})
    def test_config_bugzilla(self):
        """
        Test when the config is set for bugzilla to be the bugtracker.
        """
        bugs.set_bugtracker()

        self.assertTrue(isinstance(bugs.bugtracker, bugs.Bugzilla))

    @mock.patch('bodhi.server.bugs.bugtracker', None)
    @mock.patch.dict('bodhi.server.bugs.config', {'bugtracker': 'fake'})
    def test_config_not_bugzilla(self):
        """
        Test when the config is no set for bugzilla to be the bugtracker.
        """
        bugs.set_bugtracker()

        self.assertTrue(isinstance(bugs.bugtracker, bugs.FakeBugTracker))
