from abc import ABC
from abc import abstractmethod
import logging
import os
from typing import Dict, List

import twitter
import twitter.models

from katatachi.interface.worker import WorkContext
from katatachi.pipeline import PipelineSource

from .twitter_media import TwitterMedia


class TwitterMediaScraper(PipelineSource, ABC):
    SINCE_ID_KEY = "since_id"

    def __init__(
        self, screen_name: str, document_idempotency_key: str, starting_state: str
    ):
        super(TwitterMediaScraper, self).__init__(
            document_idempotency_key, starting_state
        )
        self.screen_name = screen_name
        # 200 is the max count we can get
        # if the user sends more than 200 tweets within a worker interval then we are going to miss some tweets
        self.count = 200
        self.twitter_api = None

    def get_id(self):
        return f"twitter_image_scraper.{self.screen_name}"

    def pre_work(self, context: WorkContext):
        self.twitter_api = twitter.api.Api(
            consumer_key=os.getenv("TWITTER_CONSUMER_KEY"),
            consumer_secret=os.getenv("TWITTER_CONSUMER_SECRET"),
            access_token_key=os.getenv("TWITTER_ACCESS_TOKEN_KEY"),
            access_token_secret=os.getenv("TWITTER_ACCESS_TOKEN_SECRET"),
        )

    @abstractmethod
    def extract_media(
        self, status: twitter.models.Status, logger: logging.Logger
    ) -> List[TwitterMedia]:
        pass

    @abstractmethod
    def media_list_to_documents(self, twitter_media: List[TwitterMedia]) -> List[Dict]:
        pass

    @abstractmethod
    def document_to_id(self, document: Dict) -> str:
        pass

    @abstractmethod
    def document_to_created_at_in_seconds(self, document: Dict) -> int:
        pass

    def process(self, context: WorkContext) -> List[Dict]:
        try:
            self.twitter_api.GetUserTimeline(screen_name=self.screen_name, count=1)
        except twitter.error.TwitterError as te:
            context.logger().warning(
                f"Fail to test getting one status for user {self.screen_name}, error {str(te)}"
            )
            if "not authorized" in str(te).lower():
                context.logger().warning(f"Unauthorized to view user {self.screen_name}, skipping")
                return []
            else:
                raise te

        # Initialize a since_id
        if not context.metadata_store().exists(TwitterMediaScraper.SINCE_ID_KEY):
            context.logger().info("since_id is not set, getting it")
            init_tweets = self.twitter_api.GetUserTimeline(
                screen_name=self.screen_name, count=1
            )
            if not init_tweets:
                raise RuntimeError(
                    "Cannot obtain the initial tweets in order to get the initial since_id"
                )
            init_since_id = max(init_tweets, key=lambda t: t.id).id
            context.metadata_store().set(
                TwitterMediaScraper.SINCE_ID_KEY, str(init_since_id)
            )

        # Get latest tweets since since_id
        since_id = int(context.metadata_store().get(TwitterMediaScraper.SINCE_ID_KEY))

        ignore_since_id = False
        try:
            self.twitter_api.GetStatus(since_id)
            context.logger().info(f"Successfully got status for since_id {since_id}")
        except twitter.error.TwitterError as te:
            context.logger().warning(
                f"Fail to get status for since_id {since_id}, ignoring, error {str(te)}"
            )
            ignore_since_id = True

        if not ignore_since_id:
            context.logger().info(
                f"since_id {since_id} is valid, getting tweets since since_id"
            )
            tweets = self.twitter_api.GetUserTimeline(
                screen_name=self.screen_name, since_id=since_id, count=self.count
            )  # type: List[twitter.models.Status]
        else:
            context.logger().info(
                f"since_id {since_id} is invalid, just getting {self.count} tweets"
            )
            tweets = self.twitter_api.GetUserTimeline(
                screen_name=self.screen_name, count=self.count
            )  # type: List[twitter.models.Status]

        if not tweets:
            context.logger().info("No tweets to process")
            return []

        # Process the tweets
        context.logger().info(f"Going to process {len(tweets)} tweets")
        new_documents = []
        for tweet in tweets:
            media_list = self.extract_media(tweet, context.logger())
            new_documents += self.media_list_to_documents(media_list)

        return new_documents

    def post_process(self, context: WorkContext, processed_documents: List[Dict]):
        if not processed_documents:
            context.logger().info("No post process happening")
            return
        # Write back a new since_id
        new_since_id = self.document_to_id(
            max(processed_documents, key=self.document_to_created_at_in_seconds)
        )
        context.metadata_store().set(
            TwitterMediaScraper.SINCE_ID_KEY, str(new_since_id)
        )
        context.logger().info(f"New since_id is set to {new_since_id}")
