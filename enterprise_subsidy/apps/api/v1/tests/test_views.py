"""
Tests for views.
"""
import json
import uuid
from functools import partial
from unittest import mock

import ddt
from openedx_ledger.models import TransactionStateChoices
from rest_framework import status
from rest_framework.reverse import reverse

from enterprise_subsidy.apps.api.v1.tests.mixins import APITestMixin
from enterprise_subsidy.apps.subsidy.models import OCM_ENROLLMENT_REFERENCE_TYPE
from enterprise_subsidy.apps.subsidy.tests.factories import SubsidyFactory
from test_utils.utils import MockResponse

SERIALIZED_DATE_PATTERN = '%Y-%m-%dT%H:%M:%SZ'


class APITestBase(APITestMixin):
    """
    Provides shared test resource setup between curation-related API test classes.

    Contains boilerplate to create a couple of subsidies with related ledgers and starting transactions.
    """

    def setUp(self):
        super().setUp()

        # Create the main test objects that the test users should be able to access.
        self.subsidy_one = SubsidyFactory(enterprise_customer_uuid=self.enterprise_uuid, starting_balance=10000)
        self.ledger_one = self.subsidy_one.ledger
        self.transaction_one = self.subsidy_one.initialize_ledger()

        # Create an extra subsidy corresponding to a different enterprise customer an unprivileged default test user
        # should not be able to access.
        self.subsidy_two = SubsidyFactory(enterprise_customer_uuid=uuid.uuid4(), starting_balance=10000)
        self.ledger_two = self.subsidy_two.ledger
        self.transaction_two = self.subsidy_two.initialize_ledger()


@ddt.ddt
class SubsidyViewSetTests(APITestBase):
    """
    Test SubsidyViewSet.
    """
    get_details_url = partial(reverse, "api:v1:subsidy-detail")
    get_list_url = partial(reverse, "api:v1:subsidy-list")

    def test_get_one_subsidy(self):
        """
        Test that a subsidy detail call returns the expected
        serialized response.
        """
        self.set_up_admin(enterprise_uuids=[self.subsidy_one.enterprise_customer_uuid])
        response = self.client.get(self.get_details_url([self.subsidy_one.uuid]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        expected_result = {
            "uuid": str(self.subsidy_one.uuid),
            "title": self.subsidy_one.title,
            "enterprise_customer_uuid": self.subsidy_one.enterprise_customer_uuid,
            "active_datetime": self.subsidy_one.active_datetime.strftime(SERIALIZED_DATE_PATTERN),
            "expiration_datetime": self.subsidy_one.expiration_datetime.strftime(SERIALIZED_DATE_PATTERN),
            "unit": self.subsidy_one.unit,
            "reference_id": self.subsidy_one.reference_id,
            "reference_type": self.subsidy_one.reference_type,
            "current_balance": self.subsidy_one.current_balance(),
        }
        self.assertEqual(expected_result, response.json())

    def test_get_one_subsidy_learner_not_allowed(self):
        """
        Test that learner roles do not allow access to read subsidies.
        """
        self.set_up_learner()
        response = self.client.get(self.get_details_url([self.subsidy_one.uuid]))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt.ddt
class TransactionViewSetTests(APITestBase):
    """
    Test TransactionViewSet.
    """

    # Uncomment this later once we have segment events firing.
    # @mock.patch('enterprise_subsidy.apps.api.v1.event_utils.track_event')
    # def test_create(self, mock_track_event):
    @mock.patch("enterprise_subsidy.apps.subsidy.models.Subsidy.enterprise_client")
    @mock.patch("enterprise_subsidy.apps.api_client.enterprise_catalog.EnterpriseCatalogApiClient.get_course_price")
    def test_create(self, mock_get_course_price, mock_enterprise_client):
        """
        Test create Transaction, happy case.
        """
        url = reverse("api:v1:transaction-list")
        test_enroll_reference_id = "test-enroll-reference-id"
        mock_enterprise_client.enroll.return_value = test_enroll_reference_id
        mock_get_course_price.return_value = "100.00"
        # Create privileged staff user that should be able to create Transactions.
        self.set_up_operator()
        post_data = {
            "subsidy_uuid": str(self.subsidy_one.uuid),
            "learner_id": 1234,
            "content_key": "course-v1:edX-test-course",
            "access_policy_uuid": str(uuid.uuid4()),
        }
        response = self.client.post(url, post_data)
        assert response.status_code == status.HTTP_201_CREATED
        create_response_data = response.json()
        assert len(create_response_data["uuid"]) == 36
        # TODO: make this assertion more specific once we hookup the idempotency_key to the request body.
        assert create_response_data["idempotency_key"]
        assert create_response_data["content_key"] == post_data["content_key"]
        assert create_response_data["lms_user_id"] == post_data["learner_id"]
        assert create_response_data["subsidy_access_policy_uuid"] == post_data["access_policy_uuid"]
        assert json.loads(create_response_data["metadata"]) == {}
        assert create_response_data["unit"] == self.ledger_one.unit
        assert create_response_data["quantity"] < 0  # No need to be exact at this time, I'm just testing create works.
        assert create_response_data["reference_id"] == test_enroll_reference_id
        assert create_response_data["reference_type"] == OCM_ENROLLMENT_REFERENCE_TYPE
        assert create_response_data["reversal"] is None
        assert create_response_data["state"] == TransactionStateChoices.COMMITTED

        # `create` was successful, so now call `retreive` to read the new Transaction and do a basic smoke test.
        detail_url = reverse("api:v1:transaction-detail", kwargs={"uuid": create_response_data["uuid"]})
        retrieve_response = self.client.get(detail_url)
        assert retrieve_response.status_code == status.HTTP_200_OK
        retrieve_response_data = retrieve_response.json()
        assert retrieve_response_data["uuid"] == create_response_data["uuid"]
        assert retrieve_response_data["idempotency_key"] == create_response_data["idempotency_key"]

        # Uncomment after Segment events are setup:
        #
        # Finally, check that a tracking event was emitted:
        # mock_track_event.assert_called_once_with(
        #     STATIC_LMS_USER_ID,
        #     SegmentEvents.TRANSACTION_CREATED,
        #     {
        #         "ledger_transaction_uuid": create_response_data["uuid"],
        #         "enterprise_customer_uuid": str(self.subsidy_one.enterprise_customer_uuid),
        #         "subsidy_uuid": str(self.curation_config_one.uuid),
        #     },
        # )

    @ddt.data("admin", "learner")
    def test_create_denied_role(self, role):
        """
        Test create Transaction, permission denied due to not being an operator.
        """
        if role == "admin":
            self.set_up_admin()
        elif role == "learner":
            self.set_up_learner()
        url = reverse("api:v1:transaction-list")
        post_data = {
            "subsidy_uuid": str(self.subsidy_one.uuid),
            "learner_id": 1234,
            "content_key": "course-v1:edX-test-course",
            "access_policy_uuid": str(uuid.uuid4()),
        }
        response = self.client.post(url, post_data)
        assert response.status_code == status.HTTP_403_FORBIDDEN
        # Just make sure there's any parseable json which is likely to contain an explanation of the error.
        assert response.json()

    def test_create_invalid_subsidy_uuid(self):
        """
        Test create Transaction, failed due to invalid uuid.
        """
        url = reverse("api:v1:transaction-list")
        # Create privileged staff user that should be able to create Transactions.
        self.set_up_operator()

        post_data = {
            "subsidy_uuid": str(self.subsidy_one.uuid) + "a",  # Make uuid invalid.
            "learner_id": 1234,
            "content_key": "course-v1:edX-test-course",
            "access_policy_uuid": str(uuid.uuid4()),
        }
        response = self.client.post(url, post_data)
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "detail" in response.json()

    def test_create_invalid_access_policy_uuid(self):
        """
        Test create Transaction, failed due to invalid uuid.
        """
        url = reverse("api:v1:transaction-list")
        # Create privileged staff user that should be able to create Transactions.
        self.set_up_operator()

        post_data = {
            "subsidy_uuid": str(self.subsidy_one.uuid),
            "learner_id": 1234,
            "content_key": "course-v1:edX-test-course",
            "access_policy_uuid": str(uuid.uuid4()) + "a",  # Make uuid invalid.
        }
        response = self.client.post(url, post_data)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Error" in response.json()

    @ddt.data("subsidy_uuid", "learner_id", "content_key", "access_policy_uuid")
    def test_create_missing_inputs(self, missing_post_arg):
        """
        Test create Transaction, 4xx due to missing inputs.
        """
        url = reverse("api:v1:transaction-list")
        # Create privileged staff user that should be able to create Transactions.
        self.set_up_operator()

        post_data = {
            "subsidy_uuid": str(self.subsidy_one.uuid),
            "learner_id": 1234,
            "content_key": "course-v1:edX-test-course",
            "access_policy_uuid": str(uuid.uuid4()),
        }
        del post_data[missing_post_arg]
        response = self.client.post(url, post_data)
        assert response.status_code >= 400 and response.status_code < 500
        # Just make sure there's any parseable json which is likely to contain an explanation of the error.
        assert response.json()


@ddt.ddt
class ContentMetadataViewSetTests(APITestBase):
    """
    Test ContentMetadataViewSet.
    """
    content_uuid_1 = str(uuid.uuid4())
    content_price_1 = 100
    content_key_1 = "edX+DemoX"
    content_uuid_2 = str(uuid.uuid4())
    content_price_2 = 200
    content_key_2 = "edX+DemoX2"
    edx_course_metadata = {
        "key": content_key_1,
        "content_type": "course",
        "uuid": content_uuid_1,
        "title": "Demonstration Course",
        "entitlements": [
            {
                "mode": "verified",
                "price": content_price_1,
                "currency": "USD",
                "sku": "8A47F9E",
                "expires": "null"
            }
        ],
        "product_source": None,
    }
    executive_education_course_metadata = {
        "key": content_key_2,
        "content_type": "course",
        "uuid": content_uuid_2,
        "title": "Demonstration Course",
        "entitlements": [
            {
                "mode": "paid-executive-education",
                "price": content_price_2,
                "currency": "USD",
                "sku": "B98DE21",
                "expires": "null"
            }
        ],
        "product_source": {
            "name": "2u",
            "slug": "2u",
            "description": "2U, Trilogy, Getsmarter -- external source for 2u courses and programs"
        },
    }
    mock_http_error_reason = 'Something Went Wrong'
    mock_http_error_url = 'foobar.com'

    @ddt.data(
        {
            'content_uuid': content_uuid_1,
            'content_key': content_key_1,
            'content_price': content_price_1,
            'mock_metadata': edx_course_metadata,
            'source': 'edX'
        },
        {
            'content_uuid': content_uuid_2,
            'content_key': content_key_2,
            'content_price': content_price_2,
            'mock_metadata': executive_education_course_metadata,
            'source': '2u'
        },
    )
    @ddt.unpack
    def test_successful_get(
        self,
        content_uuid,
        content_key,
        content_price,
        mock_metadata,
        source,
    ):
        with mock.patch(
            'enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient',
            return_value=mock.MagicMock()
        ) as mock_oauth_client:
            customer_uuid = uuid.uuid4()
            self.set_up_admin(enterprise_uuids=[str(customer_uuid)])
            mock_oauth_client.return_value.get.return_value = MockResponse(mock_metadata, 200)
            url = reverse('api:v1:content-metadata', kwargs={'content_identifier': content_key})
            response = self.client.get(url + f'?enterprise_customer_uuid={str(customer_uuid)}')
            assert response.status_code == 200
            assert response.json() == {
                'content_uuid': str(content_uuid),
                'content_key': content_key,
                'source': source,
                'content_price': content_price,
            }

            # Everything after this line is testing the view's cache
            # If this mock response is ever hit, the test will fail, caching prevents it.
            mock_oauth_client.return_value.get.side_effect = Exception("Does not reach this")
            response = self.client.get(url + f'?enterprise_customer_uuid={str(customer_uuid)}')
            assert response.status_code == 200
            assert response.json() == {
                'content_uuid': str(content_uuid),
                'content_key': content_key,
                'source': source,
                'content_price': content_price,
            }

    def test_failure_no_permission(self):
        self.set_up_admin(enterprise_uuids=[str(uuid.uuid4())])
        url = reverse('api:v1:content-metadata', kwargs={'content_identifier': self.content_key_1})
        response = self.client.get(url + f'?enterprise_customer_uuid={str(uuid.uuid4())}')
        assert response.status_code == 403
        assert response.json() == {'detail': 'MISSING: subsidy.can_read_metadata'}

    @ddt.data(
        {
            'catalog_status_code': 404,
            'expected_response': 'Content not found',
        },
        {
            'catalog_status_code': 403,
            'expected_response': f'Failed to fetch data from catalog service with exc: '
                                 f'403 Client Error: {mock_http_error_reason} for url: {mock_http_error_url}',
        },
    )
    @ddt.unpack
    def test_failure_exception_while_gather_metadata(self, catalog_status_code, expected_response):
        with mock.patch(
            'enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient',
            return_value=mock.MagicMock()
        ) as mock_oauth_client:
            customer_uuid = uuid.uuid4()
            self.set_up_admin(enterprise_uuids=[str(customer_uuid)])
            mock_oauth_client.return_value.get.return_value = MockResponse(
                {"something": "fail"},
                catalog_status_code,
                reason=self.mock_http_error_reason,
                url=self.mock_http_error_url
            )
            url = reverse('api:v1:content-metadata', kwargs={'content_identifier': 'content_key'})
            response = self.client.get(url + f'?enterprise_customer_uuid={str(customer_uuid)}')
            assert response.status_code == catalog_status_code
            assert response.json() == expected_response