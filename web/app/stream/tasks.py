from app.settings import Config
from app.worker.celery_init import celery
from celery.utils.log import get_task_logger
from app.utils.reverse_tweet_matcher import ReverseTweetMatcher
from app.utils.process_tweet import ProcessTweet
from app.utils.process_media import ProcessMedia
from app.utils.priority_queue import TweetIdQueue
from app.utils.predict_sentiment import PredictSentiment
from app.utils.project_config import ProjectConfig
from app.stream.redis_s3_queue import RedisS3Queue
from app.stream.trending_tweets import TrendingTweets
from app.stream.trending_topics import TrendingTopics
from app.stream.es_queue import ESQueue
from app.extensions import es
import logging
import os
import json

@celery.task(ignore_result=True)
def handle_tweet(tweet, send_to_es=True, use_pq=True, debug=False, store_unmatched_tweets=False):
    logger = get_task_logger(__name__)
    if debug:
        logger.setLevel(logging.DEBUG)
    # reverse match to find project
    rtm = ReverseTweetMatcher(tweet=tweet)
    candidates = rtm.get_candidates()
    if len(candidates) == 0:
        # Could not match keywords. This might occur quite frequently e.g. when tweets are collected accross different languages/keywords
        logger.info('Tweet {} could not be matched against any existing projects.'.format(tweet['id']))
        if store_unmatched_tweets:
            # store to separate file for later analysis
            config = Config()
            with open(os.path.join(config.PROJECT_ROOT, 'logs', 'reverse_match_errors', tweet['id_str'] + '.json'), 'w') as f:
                json.dump(tweet, f)
        return
    # queue up for s3 upload and add to priority queue
    logger.info("SUCCESS: Found {} project(s) ({}) as a matching project for tweet".format(len(candidates), ', '.join(candidates)))
    redis_queue = RedisS3Queue()
    es_queue = ESQueue()
    stream_config_reader = ProjectConfig()
    for project in candidates:
        stream_config = stream_config_reader.get_config_by_project(project)
        if stream_config['storage_mode'] == 'test_mode':
            logger.debug('Running in test mode. Not sending to S3 or ES.')
            return
        # add tracking info
        tweet['_tracking_info'] = stream_config_reader.get_tracking_info(project)
        tweet['_tracking_info']['matching_keywords'] = rtm.matching_keywords[project]
        # Queue up on Redis for subsequent upload
        redis_queue.push(json.dumps(tweet).encode(), project)
        # Possibly add tweet to trending tweets
        if stream_config['compile_trending_tweets']:
            trending_tweets = TrendingTweets(project, project_locales=stream_config['locales'])
            trending_tweets.process(tweet)
        # Extract trending topics
        if stream_config['compile_trending_topics']:
            trending_topics = TrendingTopics(project, project_locales=stream_config['locales'], project_keywords=stream_config['keywords'])
            trending_topics.process(tweet)
        # preprocess tweet
        pt = ProcessTweet(tweet=tweet, project=project, project_locales=stream_config['locales'])
        processed_tweet = pt.process_and_predict()
        if use_pq and pt.should_be_annotated():
            # add to Tweet ID queue for crowd labelling
            logger.info('Add tweet {} to priority queue...'.format(processed_tweet['id']))
            tid = TweetIdQueue(stream_config['es_index_name'], priority_threshold=3)
            processed_tweet['text'] = pt.anonymize_text(processed_tweet['text'])
            tid.add_tweet(processed_tweet, priority=0)
        if stream_config['image_storage_mode'] != 'inactive':
            pm = ProcessMedia(tweet, project, image_storage_mode=stream_config['image_storage_mode'])
            pm.process()
        if send_to_es and stream_config['storage_mode'] in ['s3-es', 's3-es-no-retweets']:
            if rtm.is_retweet and stream_config['storage_mode'] == 's3-es-no-retweets':
                # Do not store retweets on ES
                return
            # send to ES
            logger.debug('Pushing processed with id {} to ES queue'.format(processed_tweet['id']))
            es_queue.push(json.dumps(processed_tweet).encode(), project)

@celery.task
def predict(text, model='fasttext_v1.ftz', num_classes=3, path_to_model='.'):
    ps = PredictSentiment()
    prediction = ps.predict(text, model=model)
    return prediction
