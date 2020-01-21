import json
import math
from urllib.parse import parse_qs, urlparse

import ddt
import mock
import responses
from django.test import TestCase

from course_discovery.apps.course_metadata.data_loaders.marketing_site import CourseMarketingSiteDataLoader
from course_discovery.apps.course_metadata.data_loaders.tests import JSON, mock_data
from course_discovery.apps.course_metadata.data_loaders.tests.mixins import DataLoaderTestMixin
from course_discovery.apps.course_metadata.models import Course
from course_discovery.apps.course_metadata.tests.factories import CourseFactory

LOGGER_PATH = 'course_discovery.apps.course_metadata.data_loaders.marketing_site.logger'
MOCK_DRUPAL_REDIRECT_CSV_FILE = 'data/mock_redirect_csv.csv'


class AbstractMarketingSiteDataLoaderTestMixin(DataLoaderTestMixin):
    mocked_data = []

    @property
    def api_url(self):
        return self.partner.marketing_site_url_root

    def mock_api_callback(self, url, data):
        """ Paginate the data, one item per page. """

        def request_callback(request):
            count = len(data)

            # Use the querystring to determine which page should be returned. Default to page 1.
            # Note that the values of the dict returned by `parse_qs` are lists, hence the `[1]` default value.
            qs = parse_qs(urlparse(request.path_url).query)
            page = int(qs.get('page', [0])[0])
            page_size = 1

            body = {
                'list': [data[page]],
                'first': '{}?page={}'.format(url, 0),
                'last': '{}?page={}'.format(url, math.ceil(count / page_size) - 1),
            }

            if (page * page_size) < count - 1:
                next_page = page + 1
                next_url = '{}?page={}'.format(url, next_page)
                body['next'] = next_url

            return 200, {}, json.dumps(body)

        return request_callback

    def mock_api(self):
        bodies = self.mocked_data
        url = self.api_url + 'node.json'

        responses.add_callback(
            responses.GET,
            url,
            callback=self.mock_api_callback(url, bodies),
            content_type=JSON
        )

        return bodies

    def mock_login_response(self, failure=False):
        url = self.api_url + 'user'
        landing_url = '{base}admin'.format(base=self.api_url)
        status = 500 if failure else 302
        adding_headers = {}

        if not failure:
            adding_headers['Location'] = landing_url
        responses.add(responses.POST, url, status=status, adding_headers=adding_headers)

        responses.add(
            responses.GET,
            landing_url,
            status=(500 if failure else 200)
        )

        responses.add(
            responses.GET,
            '{root}restws/session/token'.format(root=self.api_url),
            body='test token',
            content_type='text/html',
            status=200
        )

    def mock_api_failure(self):
        url = self.api_url + 'node.json'
        responses.add(responses.GET, url, status=500)

    @responses.activate
    def test_ingest_with_api_failure(self):
        self.mock_login_response()
        self.mock_api_failure()

        with self.assertRaises(Exception):
            self.loader.ingest()

    @responses.activate
    def test_ingest_exception_handling(self):
        """ Verify the data loader properly handles exceptions during processing of the data from the API. """
        self.mock_login_response()
        api_data = self.mock_api()

        with mock.patch.object(self.loader, 'clean_strings', side_effect=Exception):
            with mock.patch(LOGGER_PATH) as mock_logger:
                self.loader.ingest()
                self.assertEqual(mock_logger.exception.call_count, len(api_data))
                calls = [mock.call('Failed to load %s.', datum['url']) for datum in api_data]
                mock_logger.exception.assert_has_calls(calls)

    @responses.activate
    def test_api_client_login_failure(self):
        self.mock_login_response(failure=True)
        with self.assertRaises(Exception):
            self.loader.marketing_api_client()

    def test_constructor_without_credentials(self):
        """ Verify the constructor raises an exception if the Partner has no marketing site credentials set. """
        self.partner.marketing_site_api_username = None
        with self.assertRaises(Exception):
            self.loader_class(self.partner, self.api_url)  # pylint: disable=not-callable


@ddt.ddt
class CourseMarketingSiteDataLoaderTests(AbstractMarketingSiteDataLoaderTestMixin, TestCase):
    loader_class = CourseMarketingSiteDataLoader
    mocked_data = mock_data.UNIQUE_MARKETING_SITE_API_COURSE_BODIES

    @mock.patch('course_discovery.apps.course_metadata.data_loaders.marketing_site.DRUPAL_REDIRECT_CSV_FILE',
                MOCK_DRUPAL_REDIRECT_CSV_FILE)
    def setUp(self):
        super().setUp()

    def get_key_from_mocked_data(self, course_dict):
        compound_key = course_dict['field_course_id'].split('/')
        return '{org}+{course}'.format(org=compound_key[0], course=compound_key[1])

    def setup_courses(self):
        # In our current world, we do not create courses from
        # marketing site data, but we need them for creating redirects.
        for course in self.mocked_data:
            title = course['field_course_course_title']['value']
            mocked = CourseFactory(key=self.get_key_from_mocked_data(course), partner=self.partner, title=title)
            mocked.set_active_url_slug('')  # force the active url slug to be the slugified title

    @responses.activate
    @ddt.data(
        # several redirects, no new slugs
        ('HarvardX+CS50x', ['course/long/path', 'different-prefix/introduction-to-computer-science', 'node/254'], []),
        # duplicate redirects, one new slug
        ('HarvardX+PH207x', ['node/354'], ['health-numbers']),
        # no new redirects
        ('HarvardX+CB22x', ['node/563'], []),
    )
    @ddt.unpack
    def test_ingest(self, course_key, redirects, url_slugs):
        self.mock_login_response()
        self.setup_courses()
        self.mock_api()

        test_course = Course.everything.get(key=course_key)
        original_slug = test_course.active_url_slug

        self.loader.ingest()
        test_course.refresh_from_db()

        # active slugs should not be affected
        self.assertEqual(test_course.active_url_slug, original_slug)

        test_course_paths = list(map(lambda x: x.value, test_course.url_redirects.all()))
        test_course_url_slugs = list(map(lambda x: x.url_slug, test_course.url_slug_history.all()))
        for redirect in redirects:
            self.assertIn(redirect, test_course_paths)
        for slug in url_slugs:
            self.assertIn(slug, test_course_url_slugs)
        self.assertEqual(test_course.url_redirects.count(), len(redirects))
