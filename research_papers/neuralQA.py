# notes: make sure to have the same requirements
# https://github.com/victordibia/neuralqa/blob/fb48f4d45d5856195baef25b4707e7b282cc364d/requirements.txt

import tensorflow as tf
import numpy as np
import time
import logging
from transformers import AutoTokenizer, TFAutoModelForQuestionAnswering
import yaml

from pydantic import BaseModel
from typing import Optional

logger = logging.getLogger(__name__)


##################################### Reader #####################################
class Reader:
    def __init__(self, model_name, model_path, model_type, **kwargs):
        self.load_model(model_name, model_path, model_type)

    def load_model(self, model_name, model_path, model_type):
        logger.info(">> Loading HF model " +
                    model_name + " from " + model_path)
        self.type = model_type
        self.name = model_name
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path, use_fast=True)
        self.model = TFAutoModelForQuestionAnswering.from_pretrained(
            model_path, from_pt=True)

##################################### BERTReader #####################################
class BERTReader(Reader):
    def __init__(self, model_name, model_path, model_type="bert", **kwargs):
        Reader.__init__(self, model_name, model_path, model_type)
        # self.load_model(model_name, model_path, model_type)

    def get_best_start_end_position(self, start_scores, end_scores):
        # print(start_scores)
        answer_start = tf.argmax(start_scores, axis=1).numpy()[0]
        answer_end = (tf.argmax(end_scores, axis=1) + 1).numpy()[0]
        return answer_start, answer_end

    def get_chunk_answer_span(self, inputs):
        start_time = time.time()
        answer_start_scores, answer_end_scores = self.model(inputs) #this line is casuign a problem

        answer_start, answer_end = self.get_best_start_end_position(
            answer_start_scores, answer_end_scores)

        answer_end = answer_end - \
            1 if answer_end == answer_end_scores.shape[1] else answer_end

        answer_start_softmax_probability = tf.nn.softmax(
            answer_start_scores, axis=1).numpy()[0][answer_start]
        answer_end_softmax_probability = tf.nn.softmax(
            answer_end_scores, axis=1).numpy()[0][answer_end]

        answer = self.tokenizer.decode(
            inputs["input_ids"][0][answer_start:answer_end], skip_special_tokens=True)

        # if model predict first token 0 which is in the question as part of the answer, return nothing
        if answer_start == 0:
            answer = ""

        elapsed_time = time.time() - start_time
        return {"answer": answer, "took": elapsed_time,
                "start_probability": str(answer_start_softmax_probability),
                "end_probability": str(answer_end_softmax_probability),
                "probability": str(answer_end_softmax_probability + answer_start_softmax_probability / 2)
                }

    def token_chunker(self, question, context, max_chunk_size=512, stride=2, max_num_chunks=5):
        # we tokenize question and context once.
        # if question + context > max chunksize, we break it down into multiple chunks of question +
        # subsets of context with some stride overlap

        question_tokens = self.tokenizer.encode(question)
        context_tokens = self.tokenizer.encode(
            context, add_special_tokens=False)

        chunk_holder = []
        chunk_size = max_chunk_size - len(question_tokens) - 1
        # -1 for the 102 end token we append later
        current_pos = 0
        chunk_count = 0
        while current_pos < len(context_tokens) and current_pos >= 0:

            # we want to cap the number of chunks we create
            if max_num_chunks and chunk_count >= max_num_chunks:
                break

            end_point = current_pos + \
                chunk_size if (current_pos + chunk_size) < len(context_tokens) - \
                1 else len(context_tokens) - 1
            token_chunk = question_tokens + \
                context_tokens[current_pos: end_point] + [102]

            # question type is 0, context type is 1, convert to tf
            token_type_ids = [0]*len(question_tokens) + \
                [1] * (len(token_chunk) - len(question_tokens))
            token_type_ids = tf.constant(
                token_type_ids, dtype='int32', shape=(1, len(token_type_ids)))

            # attend to every token
            attention_mask = tf.ones(
                (1, len(token_chunk)),  dtype=tf.dtypes.int32)

            # convert token chunk to tf
            token_chunk = tf.constant(
                token_chunk, dtype='int32', shape=(1, len(token_chunk)))

            chunk_holder.append(
                {"token_ids": token_chunk,
                 "context": self.tokenizer.decode(context_tokens[current_pos: end_point], skip_special_tokens=True),
                 "attention_mask":  attention_mask,
                 "token_type_ids": token_type_ids
                 })
            current_pos = current_pos + chunk_size - stride + 1
            chunk_count += 1

        return chunk_holder

    def answer_question(self, question, context, max_chunk_size=512, stride=70):

        # chunk tokens
        chunked_tokens = self.token_chunker(
            question, context, max_chunk_size, stride)
        answer_holder = []
        for chunk in chunked_tokens:
            model_input = {"input_ids": chunk["token_ids"], "attention_mask":
                           chunk["attention_mask"], "token_type_ids": chunk["token_type_ids"]}
            answer = self.get_chunk_answer_span(model_input)
            if len(answer["answer"]) > 2:
                answer["question"] = question
                answer["context"] = chunk["context"].replace("##", "").replace(
                    answer["answer"], " <em>" + answer["answer"] + "</em> ")
                answer_holder.append(answer)
        return answer_holder

    def get_correct_span_mask(self, correct_index, token_size):
        span_mask = np.zeros((1, token_size))
        span_mask[0, correct_index] = 1
        span_mask = tf.constant(span_mask, dtype='float32')
        return span_mask

    def get_embedding_matrix(self):
        if "DistilBert" in type(self.model).__name__:
            return self.model.distilbert.embeddings.word_embeddings
        else:
            return self.model.bert.embeddings.word_embeddings

    # move this to some utils file
    def clean_tokens(self, gradients, tokens, token_types):
        """
        Clean the tokens and  gradients
        Remove "[CLS]","[CLR]", "[SEP]" tokens
        Reduce (mean) gradients values for tokens that are split ##
        """
        token_holder = []
        token_type_holder = []
        gradient_holder = []
        i = 0
        while i < len(tokens):
            if (tokens[i] not in ["[CLS]", "[CLR]", "[SEP]"]):
                token = tokens[i]
                conn = gradients[i]
                token_type = token_types[i]
                if i < len(tokens)-1:
                    if tokens[i+1][0:2] == "##":
                        token = tokens[i]
                        conn = gradients[i]
                        j = 1
                        while i < len(tokens)-1 and tokens[i+1][0:2] == "##":
                            i += 1
                            token += tokens[i][2:]
                            conn += gradients[i]
                            j += 1
                        conn = conn / j
                token_holder.append(token)
                token_type_holder.append(token_type)
                # gradient_holder.append(conn)
                gradient_holder.append(
                    {"gradient": conn, "token": token, "token_type": token_type})
            i += 1
        return gradient_holder

    def get_gradient(self, question, context):
        """Return gradient of input (question) wrt to model output span prediction
        Args:
            question (str): text of input question
            context (str): text of question context/passage
            model (QA model): Hugging Face BERT model for QA transformers.modeling_tf_distilbert.TFDistilBertForQuestionAnswering, transformers.modeling_tf_bert.TFBertForQuestionAnswering
            tokenizer (tokenizer): transformers.tokenization_bert.BertTokenizerFast 
        Returns:
            (tuple): (gradients, token_words, token_types, answer_text)
        """

        embedding_matrix = self.get_embedding_matrix()

        encoded_tokens = self.tokenizer.encode_plus(
            question, context, add_special_tokens=True, return_token_type_ids=True, return_tensors="tf")
        token_ids = list(encoded_tokens["input_ids"].numpy()[0])
        vocab_size = embedding_matrix.get_shape()[0]

        # convert token ids to one hot. We can't differentiate wrt to int token ids hence the need for one hot representation
        token_ids_tensor = tf.constant([token_ids], dtype='int32')
        token_ids_tensor_one_hot = tf.one_hot(token_ids_tensor, vocab_size)

        with tf.GradientTape(watch_accessed_variables=False) as tape:
            # (i) watch input variable
            tape.watch(token_ids_tensor_one_hot)

            # multiply input model embedding matrix; allows us do backprop wrt one hot input
            inputs_embeds = tf.matmul(
                token_ids_tensor_one_hot, embedding_matrix)

            # (ii) get prediction
            start_scores, end_scores = self.model(
                {"inputs_embeds": inputs_embeds, "token_type_ids": encoded_tokens["token_type_ids"], "attention_mask": encoded_tokens["attention_mask"]})
            answer_start, answer_end = self.get_best_start_end_position(
                start_scores, end_scores)

            start_output_mask = self.get_correct_span_mask(
                answer_start, len(token_ids))
            end_output_mask = self.get_correct_span_mask(
                answer_end, len(token_ids))

            # zero out all predictions outside of the correct span positions; we want to get gradients wrt to just these positions
            predict_correct_start_token = tf.reduce_sum(
                start_scores * start_output_mask)
            predict_correct_end_token = tf.reduce_sum(
                end_scores * end_output_mask)

            # (iii) get gradient of input with respect to both start and end output
            gradient_non_normalized = tf.norm(
                tape.gradient([predict_correct_start_token, predict_correct_end_token], token_ids_tensor_one_hot), axis=2)

            # (iv) normalize gradient scores and return them as "explanations"
            gradient_tensor = (
                gradient_non_normalized /
                tf.reduce_max(gradient_non_normalized)
            )
            gradients = gradient_tensor[0].numpy().tolist()

            token_words = self.tokenizer.convert_ids_to_tokens(token_ids)
            token_types = list(
                encoded_tokens["token_type_ids"].numpy()[0].tolist())
            answer_text = self.tokenizer.decode(
                token_ids[answer_start:answer_end],  skip_special_tokens=True)

            # clean up gradients and words
            gradients = self.clean_tokens(
                gradients, token_words, token_types)
            return gradients, answer_text, question

    def explain_model(self, question, context, explain_method="gradient"):
        if explain_method == "gradient":
            return self.get_gradient(question, context)

##################################### ReaderPool #####################################
class ReaderPool():
    def __init__(self, models):
        self._selected_model = models["selected"]
        self.reader_pool = {}
        for model in models["options"]:
            if (model["type"] == "bert" or model["type"] == "distilbert"):
                self.reader_pool[model["value"]] = BERTReader(
                    model["name"], model["value"])

    @property
    def model(self):
        return self.reader_pool[self.selected_model]

    @property
    def selected_model(self):
        return self._selected_model

    @selected_model.setter
    def selected_model(self, selected_model):

        if (selected_model in self.reader_pool):
            self._selected_model = selected_model
        else:
            if (len(self.reader_pool) > 0):
                default_model = next(iter(self.reader_pool))
                logger.info(
                    ">> Model you are attempting to use %s does not exist in model pool. Using the following default model instead %s ", selected_model, default_model)
                self._selected_model = default_model
            else:
                logger.info(
                    ">> No reader has been specified in config.yaml.")
                self._selected_model = None

##################################### Answer #####################################
class Answer(BaseModel):

    max_documents: Optional[int] = 5
    query: str = "what is a fourth amendment right violation?"
    fragment_size: int = 250
    tokenstride: int = 50
    context: Optional[str] = "The fourth amendment kind of protects the rights of citizens .. such that they dont get searched"
    reader: str = None
    relsnip: bool = True
    expander: Optional[str] = None
    expansionterms: Optional[list] = None
    retriever: Optional[str] = "manual"


##################################### Explanation #####################################
class Explanation(BaseModel):
    query: str = "what is a fourth amendment right violation?"
    context: str = "The fourth amendment kind of protects the rights of citizens .. such that they dont get searched"

##################################### main #####################################
#--------config file---------------
# config_file = "research_papers/config.yaml"
config_file = "config.yaml"
with open(config_file, 'r') as f:
        app_config = yaml.load(f, Loader=yaml.FullLoader)

print(app_config["reader"])
reader_pool = ReaderPool(app_config["reader"])

#-----------get_answers(params: Answer)---------------server/routehandlers.py
# answer question based on provided context
def get_answers(params: Answer):
    """
    Used BERT Model to identify exact answer spans
                Returns:
                    [type] -- [description]
    """

    answer_holder = []
    response = {}
    start_time = time.time()

    answers = reader_pool.model.answer_question(params.query, params.context, stride=params.tokenstride)
    for answer in answers:
            answer["index"] = 0
            answer_holder.append(answer)

    elapsed_time = time.time() - start_time
    response = {"answers": answer_holder,
                "took": elapsed_time}
    return response

params = Answer()
# params.tokenstride = 0
params.query = app_config["samples"][0]['question']
params.context = app_config["samples"][0]['context']

response = get_answers(params)
print("-"*30, "GET ANSWER")
print(response)

#------------get_explanation(params: Explanation)------------------ server/routehandlers.py
def get_explanation(params: Explanation):
    """Return  an explanation for a given model
    Returns:
        [dictionary]: [explanation , query, question, ]
    """

    # TODO: Do we need to switch readers here?

    context = params.context.replace(
        "<em>", "").replace("</em>", "")

    gradients, answer_text, question = reader_pool.model.explain_model(
        params.query, context)

    explanation_result = {"gradients": gradients,
                            "answer": answer_text,
                            "question": question
                            }
    return explanation_result

params = Explanation()
params.query = app_config["samples"][0]['question']
params.context = app_config["samples"][0]['context']
explanation_result = get_explanation(params)

print("-"*30, "GET EXPLANATION")
print(explanation_result)