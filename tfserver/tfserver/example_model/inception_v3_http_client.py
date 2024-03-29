import argparse
import json
import os
import threading
from datetime import datetime

import grpc
import numpy as np
import requests
import tensorflow as tf
from tensorflow_serving.apis import predict_pb2
from tensorflow_serving.apis import prediction_service_pb2_grpc


class _ResultCounter(object):
    """Counter for the prediction results."""

    def __init__(self, num_tests, concurrency):
        self._num_tests = num_tests
        self._concurrency = concurrency
        self._error = 0
        self._done = 0
        self._active = 0
        self._condition = threading.Condition()

    def inc_error(self):
        with self._condition:
            self._error += 1

    def inc_done(self):
        with self._condition:
            self._done += 1
            self._condition.notify()

    def dec_active(self):
        with self._condition:
            self._active -= 1
            self._condition.notify()

    def get_error_rate(self):
        with self._condition:
            while self._done != self._num_tests:
                self._condition.wait()
            return self._error / float(self._num_tests)

    def throttle(self):
        with self._condition:
            while self._active == self._concurrency:
                self._condition.wait()
            self._active += 1


def _create_rpc_callback(filename, label, result_counter):
    """Creates RPC callback function.
    Args:
      label: The correct label for the predicted example.
      result_counter: Counter for the prediction result.
    Returns:
      The callback function.
    """

    def _callback(result_future):
        """Callback function.
        Calculates the statistics for the prediction result.
        Args:
          result_future: Result future of the RPC.
        """
        exception = result_future.exception()
        if exception:
            result_counter.inc_error()
            print(exception)
        else:
            response = np.array(
                result_future.result().outputs['scores'].float_val)
            prediction = np.argmax(response)
            # print(filename[0], prediction, label, response[prediction])
            # sys.stdout.write('.')
            # sys.stdout.flush()
            if args.save:
                with open("cpu_output.txt", "a+") as f:
                    f.writelines(filename[0] + " " + str(prediction) + "\n")
            if label != prediction:
                result_counter.inc_error()
        result_counter.inc_done()
        result_counter.dec_active()

    return _callback


class DataSet(object):
    """Class encompassing test, validation and training MNIST data set."""

    def __init__(self, filenames, images, labels):
        assert images.shape[0] == labels.shape[0], (
                'images.shape: %s labels.shape: %s' % (images.shape,
                                                       labels.shape))
        self._num_examples = images.shape[0]

        # Convert from [0, 255] -> [0.0, 1.0].
        images = images.astype(np.float32)

        self._images = images
        self._labels = labels
        self._filenames = filenames
        self._epochs_completed = 0
        self._index_in_epoch = 0

    @property
    def images(self):
        return self._images

    @property
    def labels(self):
        return self._labels

    @property
    def num_examples(self):
        return self._num_examples

    @property
    def epochs_completed(self):
        return self._epochs_completed

    def next_batch(self, batch_size):
        """Return the next `batch_size` examples from this data set."""
        start = self._index_in_epoch
        self._index_in_epoch += batch_size
        if self._index_in_epoch > self._num_examples:
            # Finished epoch
            self._epochs_completed += 1
            # Shuffle the data
            perm = np.arange(self._num_examples)
            # np.random.shuffle(perm)
            self._images = self._images[perm]
            self._labels = self._labels[perm]
            # Start next epoch
            start = 0
            self._index_in_epoch = batch_size
            assert batch_size <= self._num_examples
        end = self._index_in_epoch
        return self._filenames[start:end], self._images[start:end], self._labels[start:end]


def read_imagenet_val_data_sets(data_dir, num_images):
    validation_label_file = os.path.join(data_dir, "validation_label.txt")
    file_names = []
    images = []
    labels = []
    with open(validation_label_file, "r") as f:
        validation_label_lines = f.readlines()
    for line in validation_label_lines[0:num_images]:
        arr = line.split(" ")
        image_file = os.path.join(data_dir, "val/" + arr[0])
        # image_file = os.path.join(data_dir, "" + arr[0])
        label = int(arr[1])
        image_array = read_tensor_from_image_file(image_file)
        file_names.append(arr[0])
        images.append(image_array)
        labels.append(label)
    val_data_set = DataSet(file_names, np.array(images), np.array(labels))
    return val_data_set


def read_tensor_from_image_file(file_name,
                                input_height=299,
                                input_width=299,
                                input_mean=127.5,
                                input_std=127.5):
    input_name = "file_reader"
    file_reader = tf.read_file(file_name, input_name)
    if file_name.endswith(".png"):
        image_reader = tf.image.decode_png(file_reader, channels=3, name="png_reader")
    elif file_name.endswith(".gif"):
        image_reader = tf.squeeze(tf.image.decode_gif(file_reader, name="gif_reader"))
    elif file_name.endswith(".bmp"):
        image_reader = tf.image.decode_bmp(file_reader, name="bmp_reader")
    else:
        image_reader = tf.image.decode_jpeg(
            file_reader, channels=3, name="jpeg_reader")
    float_caster = tf.cast(image_reader, tf.float32)
    dims_expander = tf.expand_dims(float_caster, 0)
    resized = tf.image.resize_bilinear(dims_expander, [input_height, input_width])
    normalized = tf.divide(tf.subtract(resized, [input_mean]), [input_std])
    # with tf.Session() as sess:
    result = sess.run(normalized)
    result = result.reshape([input_height, input_width, 3])
    return result


def grpc_inception_v3_client(target, model_name, num_tests, concurrency, data_dir):
    test_data_set = read_imagenet_val_data_sets(data_dir, num_tests)
    start = datetime.now()
    result_counter = _ResultCounter(num_tests, concurrency)
    channel = grpc.insecure_channel(target)
    stub = prediction_service_pb2_grpc.PredictionServiceStub(channel)
    for _ in range(num_tests):
        filename, image, label = test_data_set.next_batch(1)
        request = predict_pb2.PredictRequest()
        request.model_spec.name = model_name
        request.model_spec.signature_name = 'predict_images'
        request.inputs['images'].CopyFrom(tf.make_tensor_proto(image[0], shape=[1, 299, 299, 3]))
        result_counter.throttle()
        result_future = stub.Predict.future(request, 5.0)  # 5 seconds
        result_future.add_done_callback(
            _create_rpc_callback(filename, label[0], result_counter))
    deltaTime = (datetime.now() - start).total_seconds()
    print("Time:", deltaTime)
    return result_counter.get_error_rate()


def http_inception_v3_client(target, model_name, num_tests, data_dir):
    test_data_set = read_imagenet_val_data_sets(data_dir, num_tests)
    url = "http://" + target + "/v1/models/" + model_name + ":predict"
    start = datetime.now()
    for _ in range(num_tests):
        filename, image, label = test_data_set.next_batch(1)
        request = {
            "signature_name": 'predict_images',
            "instances": image.reshape([1, 299, 299, 3]).tolist()
        }
        response = requests.post(url, data=json.dumps(request))
        # print(response.json())
    deltaTime = (datetime.now() - start).total_seconds()
    print("Time:", deltaTime)


def http_inception_v3_kfserving_client(target, model_name, num_tests, data_dir):
    test_data_set = read_imagenet_val_data_sets(data_dir, num_tests)
    url = "http://" + target + "/v1/models/" + model_name + ":predict"
    start = datetime.now()
    for _ in range(num_tests):
        filename, image, label = test_data_set.next_batch(1)
        request = {
            "instances": image.reshape([1, 299, 299, 3]).tolist()
        }
        response = requests.post(url, data=json.dumps(request))
        # print(response.json())
    deltaTime = (datetime.now() - start).total_seconds()
    print("Time:", deltaTime)


def generator_wrk_post_lua():
    image = np.random.normal(-1, 1, [1, 299, 299, 3])

    request = {
        "instances": image.reshape([1, 299, 299, 3]).tolist()
    }
    data=json.dumps(request)
    with open("incepiton_v3_post.lua","w+") as f:
        f.writelines('wrk.method = "POST"\n')
        f.write("wrk.body = '")
        f.write(data)
        f.write("'\n")
        f.writelines('wrk.headers["Content-Type"] = "application/json"')



if __name__ == '__main__':
    import sys
    # generator_wrk_post_lua()
    # sys.exit(0)
    sess = tf.Session()
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, help="host:port")
    parser.add_argument("--model_name", default="inception_v3", help="host:port")
    parser.add_argument("--num_tests", type=int, default=100, help="num of test images")
    parser.add_argument("--concurrency", type=int, default=1, help="concurrency of threads")
    parser.add_argument("--data_dir", required=True, help="dataset dir")
    parser.add_argument("--save", default=False, help="dataset dir")
    args = parser.parse_args()

    http_inception_v3_kfserving_client(args.host, args.model_name, args.num_tests, args.data_dir)
    # python ./inception_v3_http_client.py --model_name=inception_v3 --host=127.0.0.1:8080 --data_dir=/home/Cambricon-Zy/datasets/data_test --num_tests=100 --concurrency=1
    # ('Time:', 54.399587)
