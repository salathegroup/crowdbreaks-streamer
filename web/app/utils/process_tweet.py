import math
import re
import logging
from app.utils.predict_sentiment import PredictSentiment
from helpers import report_error
import unicodedata
import random

class ProcessTweet(object):
    """Wrapper class for functions to process/modify tweets"""

    # Fields to extract from tweet object
    KEEP_FIELDS = [
            'id',
            'created_at',
            'text',
            'lang',
            'coordinates',
            'timestamp_ms',
            {
                'place': [
                    'id',
                    'place_type',
                    'full_name',
                    'country',
                    'country_code',
                    'place_type'
                ]
            },
            {
                'entities': ['hashtags']
            },
            {
                'user': [
                    'description',
                    'screen_name',
                    'id_str',
                    'lang',
                    'name',
                    'location',
                    'time_zone',
                    'geo_enabled'
                ]
            }
        ]


    def __init__(self, project=None, tweet=None, project_locales=None):
        self.tweet = tweet            # initial tweet
        if self.tweet is None:
            self.extended_tweet = None
        else:
            self.extended_tweet = self._get_extended_tweet()
        self.processed_tweet = None   # processed tweet
        self.logger = logging.getLogger(__name__)
        self.project = project
        if not isinstance(project_locales, list):
            self.project_locales = []
        else:
            self.project_locales = project_locales
        self.control_char_regex = r'[\r\n\t]+'

    @property
    def is_retweet(self):
        return 'retweeted_status' in self.tweet

    @property
    def has_quoted_status(self):
        return 'quoted_status' in self.tweet

    @property
    def is_possibly_sensitive(self):
        if 'possibly_sensitive' in self.tweet:
            return self.tweet['possibly_sensitive']
        else:
            return False

    def is_matching_project_locales(self):
        if len(self.project_locales) == 0:
            # We have no project language information
            return True
        return self.tweet['lang'] in self.project_locales

    def should_be_annotated(self):
        if self.is_retweet or self.has_quoted_status or self.is_possibly_sensitive:
            return False
        if not self.is_matching_project_locales():
            return False
        try:
            model = list(self.processed_tweet['meta']['sentiment'].keys())[0]
            probability = self.processed_tweet['meta']['sentiment'][model]['probability']
        except KeyError:
            pass
        else:
            # discard with prediction probability
            r = random.random()
            if r < probability:
                return False
        return True

    def process_and_predict(self):
        _ = self.process()
        self.predict()
        return self.get_processed_tweet()

    def process(self):
        # reduce to only certain fields
        self.strip()
        # add is_retweet field
        self.add_retweet_info()
        # compute average location from bounding box (reducing storage on ES)
        if self.tweet['place'] is not None and self.tweet['place']['bounding_box'] is not None:
            self.compute_average_location()
            self.logger.debug('Computed average location {} and average radius {}'.format(self.processed_tweet['place']['average_location'],
                self.processed_tweet['place']['location_radius']))
        return self.get_processed_tweet()

    def predict(self):
        if self.project == 'vaccine-sentiment-tracking' and 'text' in self.KEEP_FIELDS:
            ps = PredictSentiment()
            model = 'fasttext_v1.ftz'
            prediction = ps.predict(self.processed_tweet['text'], model=model)
            if prediction is not None:
                meta = {'sentiment': {str(model.split('.')[0]): {'label': prediction['labels'][0], 'label_val': prediction['label_vals'][0], 'probability': prediction['probabilities'][0]}}}
                self.logger.debug('meta: {}'.format(meta))
                self.add_meta(meta)

    def strip(self):
        """Strip fields before sending to ElasticSearch
        """
        tweet_stripped = {}
        if self.processed_tweet is not None:
            tweet_stripped = self.processed_tweet
        for key in self.KEEP_FIELDS:
            if isinstance(key, dict):
                nested_key, nested_values = list(key.items())[0]
                if nested_key in self.tweet and self.tweet[nested_key] is not None:
                    for val in nested_values:
                        if val in self.tweet[nested_key] and self.tweet[nested_key][val] is not None:
                            try:
                                tweet_stripped[nested_key][val] = self.tweet[nested_key].get(val, None)
                            except KeyError:
                                tweet_stripped[nested_key] = {}
                                tweet_stripped[nested_key][val] = self.tweet[nested_key].get(val, None)
            else:
                tweet_stripped[key] = self.tweet.get(key, None)
        if 'text' in self.KEEP_FIELDS:
            tweet_stripped['text'] = self.get_text()
        self.processed_tweet = tweet_stripped

    def compute_average_location(self):
        """Compute average location from bounding box
        """
        if self.tweet is None:
            return None
        coords = self.tweet.get('place', {}).get('bounding_box', {}).get('coordinates', None)
        if coords is None:
            return
        parsed_coords = []
        for lon_d, lat_d in coords[0]:
            parsed_coords.append([float(lon_d), float(lat_d)])
        av_x = av_y = av_z = 0.0
        for lon_d, lat_d in parsed_coords:
            # convert to radian
            lon = lon_d * math.pi / 180.0
            lat = lat_d * math.pi / 180.0
            # transform to cartesian coords and sum up
            av_x += math.cos(lat) * math.cos(lon)
            av_y += math.cos(lat) * math.sin(lon)
            av_z += math.sin(lat)
        # normalize
        num_points = len(parsed_coords)
        av_x /= num_points
        av_y /= num_points
        av_z /= num_points
        # transform back to polar coordinates
        av_lat = (180 / math.pi) * math.atan2(av_z, math.sqrt(av_x * av_x + av_y * av_y))
        av_lon = (180 / math.pi) * math.atan2(av_y, av_x)
        # calculate approximate radius if polygon is approximated to be a circle (for better estimate, calculate area)
        max_lat = max([lat for lon, lat in parsed_coords])
        max_lon = max([lon for lon, lat in parsed_coords])
        radius = (abs(av_lon - max_lon) + abs(av_lat - max_lat))/2
        # store in target object
        if 'place' not in self.processed_tweet:
            self.processed_tweet['place'] = {}
        self.processed_tweet['place']['average_location'] = [av_lon, av_lat]
        self.processed_tweet['place']['location_radius'] = radius

    def add_meta(self, meta):
        if self.processed_tweet is None:
            self.error('Cannot add meta to empty tweet.')
            return
        if 'meta' not in self.processed_tweet:
            self.processed_tweet['meta'] = {}
        if not isinstance(meta, dict):
            self.error('To be added meta must be a dictionary.')
        # merge with existing meta
        self.processed_tweet['meta'] = {**self.processed_tweet['meta'], **meta}

    def add_retweet_info(self):
        if self.tweet is None:
            return
        self.processed_tweet['is_retweet'] = self.is_retweet

    def get_processed_tweet(self):
        """get_processed_tweet"""
        if self.tweet is None:
            return None
        if self.processed_tweet is None:
            return self.tweet
        else:
            return self.processed_tweet

    def error(self, msg):
        report_error(self.logger, msg=msg)

    def remove_control_characters(self, s):
        if not isinstance(s, str):
            return s
        # replace \t, \n and \r characters by a whitespace
        s = re.sub(self.control_char_regex, ' ', s)
        # removes all other control characters and the NULL byte (which causes issues when parsing with pandas)
        return "".join(ch for ch in s if unicodedata.category(ch)[0]!="C")

    def get_text(self):
        """Get full text (for both retweets and normal tweets)"""
        tweet_text = ''
        if self.is_retweet:
            prefix = self._get_retweet_prefix()
            tweet_text = prefix + self._get_full_text(self.tweet['retweeted_status'])
        else:
            tweet_text = self._get_full_text(self.tweet)
        return self.remove_control_characters(str(tweet_text))

    def anonymize_text(self, tweet_text):
        tweet_text = self.replace_user_mentions(tweet_text)
        tweet_text = self.replace_urls(tweet_text)
        return tweet_text

    def replace_user_mentions(self, tweet_text):
        """Replaces @user mentions in tweet text based on indices provided in entities.user_mentions.indices"""
        filler = '@<user>'
        corr = 0
        try:
            user_mentions = self.extended_tweet['entities']['user_mentions']
        except KeyError:
            user_mentions = []
        for m in user_mentions:
            s, e = m['indices']
            s -= corr
            e -= corr
            tweet_text = tweet_text[:s] + filler + tweet_text[e:]
            corr += (e-s) - len(filler)
        return tweet_text

    def replace_urls(self, tweet_text):
        return re.sub('((www\.[^\s]+)|(https?://[^\s]+)|(http?://[^\s]+))','<url>', tweet_text)


    # private methods

    def _get_extended_tweet(self):
        if 'extended_tweet' in self.tweet:
            return self.tweet['extended_tweet']
        else:
            return self.tweet

    def _get_full_text(self, tweet_obj):
        if 'extended_tweet' in tweet_obj:
            return tweet_obj['extended_tweet']['full_text']
        else:
            return tweet_obj['text']

    def _get_retweet_prefix(self):
        m = re.match(r'^RT (@\w+): ', self.tweet['text'])
        try:
            return m[0]
        except TypeError:
            return ''
