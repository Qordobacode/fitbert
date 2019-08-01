from collections import defaultdict
from typing import Dict, List, Tuple, Union, overload

import torch
from fitbert.utils import mask as _mask
from functional import pseq, seq
from pytorch_pretrained_bert import BertForMaskedLM, tokenization


class FitBertT:
    def __init__(
        self,
        model=None,
        tokenizer=None,
        model_name="bert-large-uncased",
        mask_token="***mask***",
        disable_gpu=False,
    ):
        self.mask_token = mask_token
        self.device = torch.device(
            "cuda" if torch.cuda.is_available() and not disable_gpu else "cpu"
        )
        print("using model:", model_name)
        print("device:", self.device)
        if not model:
            self.bert = BertForMaskedLM.from_pretrained(model_name)
            self.bert.to(self.device)
        else:
            self.bert = model
        if not tokenizer:
            self.tokenizer = tokenization.BertTokenizer.from_pretrained(model_name)
        else:
            self.tokenizer = tokenizer
        self.bert.eval()

    @staticmethod
    def softmax(x):
        return x.exp() / (x.exp().sum(-1)).unsqueeze(-1)

    @staticmethod
    def is_multi(options: List[str]) -> bool:
        return seq(options).filter(lambda x: len(x.split()) != 1).non_empty()

    def mask(self, s: str, span: Tuple[int, int]) -> Tuple[str, str]:
        return _mask(s, span, mask_token=self.mask_token)

    def tokens_to_masked_ids(self, tokens, mask_ind):
        masked_tokens = tokens[:]
        masked_tokens[mask_ind] = "[MASK]"
        masked_tokens = ["[CLS]"] + masked_tokens + ["[SEP]"]
        masked_ids = self.tokenizer.convert_tokens_to_ids(masked_tokens)
        return masked_ids

    def get_sentence_probability(self, sent: str) -> float:

        tokens = self.tokenizer.tokenize(sent)
        input_ids = (
            seq(tokens)
            .enumerate()
            .starmap(lambda i, x: self.tokens_to_masked_ids(tokens, i))
            .list()
        )

        tens = torch.LongTensor(input_ids).to(self.device)
        with torch.no_grad():
            preds = self.bert(tens)
            probs = self.softmax(preds)
            tokens_ids = self.tokenizer.convert_tokens_to_ids(tokens)
            prob = (
                seq(tokens_ids)
                .enumerate()
                .starmap(lambda i, x: float(probs[i][i + 1][x].item()))
                .reduce(lambda x, y: x * y, 1)
            )

            del tens, preds, probs, tokens, input_ids
            if self.device == "cuda":
                torch.cuda.empty_cache()

            return prob

    def guess_single(self, masked_sent: str) -> List[str]:

        pre, post = masked_sent.split(self.mask_token)

        tokens = ["[CLS]"] + self.tokenizer.tokenize(pre)
        target_idx = len(tokens)
        tokens += ["[MASK]"]
        tokens += self.tokenizer.tokenize(post) + ["[SEP]"]

        input_ids = self.tokenizer.convert_tokens_to_ids(tokens)
        tens = torch.LongTensor(input_ids).unsqueeze(0)
        tens = tens.to(self.device)
        with torch.no_grad():
            preds = self.bert(tens)
            probs = self.softmax(preds)

            pred_idx = int(torch.argmax(probs[0, target_idx]).item())
            pred_tok = self.tokenizer.convert_ids_to_tokens([pred_idx])[0]

            del pred_idx, tens, preds, probs, input_ids, tokens
            if self.device == "cuda":
                torch.cuda.empty_cache()
            return pred_tok

    def rank_single(self, masked_sent: str, words: List[str]) -> List[str]:

        pre, post = masked_sent.split(self.mask_token)

        tokens = ["[CLS]"] + self.tokenizer.tokenize(pre)
        target_idx = len(tokens)
        tokens += ["[MASK]"]
        tokens += self.tokenizer.tokenize(post) + ["[SEP]"]

        words_ids = (
            seq(words)
            .map(lambda x: self.tokenizer.tokenize(x))
            .map(lambda x: self.tokenizer.convert_tokens_to_ids(x)[0])
        )

        input_ids = self.tokenizer.convert_tokens_to_ids(tokens)
        tens = torch.LongTensor(input_ids).unsqueeze(0)
        tens = tens.to(self.device)
        with torch.no_grad():
            preds = self.bert(tens)
            probs = self.softmax(preds)

            ranked_options = (
                seq(words_ids)
                .map(lambda x: float(probs[0][target_idx][x].item()))
                .zip(words)
                .sorted(key=lambda x: x[0], reverse=True)
                .map(lambda x: x[1])
            ).list()

            del tens, preds, probs, tokens, words_ids, input_ids
            if self.device == "cuda":
                torch.cuda.empty_cache()
            return ranked_options

    def rank_multi(self, masked_sent: str, options: List[str]) -> List[str]:
        ranked_options = (
            seq(options)
            .map(lambda x: masked_sent.replace(self.mask_token, x))
            .map(lambda x: self.get_sentence_probability(x))
            .zip(options)
            .sorted(key=lambda x: x[0], reverse=True)
            .map(lambda x: x[1])
        ).list()

        return ranked_options

    def simplify_options(self, sent: str, options: List[str]):

        options_split = seq(options).map(lambda x: x.split())

        trans_start = list(zip(*options_split))

        start = (
            seq(trans_start)
            .take_while(lambda x: seq(x).distinct().len() == 1)
            .map(lambda x: x[0])
            .list()
        )

        options_split_reversed = seq(options_split).map(
            lambda x: seq(x[len(start) :]).reverse()
        )

        trans_end = list(zip(*options_split_reversed))

        end = (
            seq(trans_end)
            .take_while(lambda x: seq(x).distinct().len() == 1)
            .map(lambda x: x[0])
            .list()
        )

        start_words = seq(start).make_string(" ")
        end_words = seq(end).reverse().make_string(" ")

        options = (
            seq(options_split)
            .map(lambda x: x[len(start) : len(x) - len(end)])
            .map(lambda x: seq(x).make_string(" ").strip())
            .list()
        )

        sub = seq([start_words, self.mask_token, end_words]).make_string(" ").strip()
        sent = sent.replace(self.mask_token, sub)

        return options, sent, start_words, end_words

    def rank(self, sent: str, options: List[str]) -> str:

        options = seq(options).distinct()

        if seq(options).len() == 1:
            return options.list()

        options, sent, start_words, end_words = self.simplify_options(sent, options)

        if self.is_multi(options):
            ranked = self.rank_multi(sent, options)
        else:
            ranked = self.rank_single(sent, options)

        ranked = (
            seq(ranked)
            .map(lambda x: [start_words, x, end_words])
            .map(lambda x: seq(x).make_string(" ").strip())
            .list()
        )

        return ranked

    def fitb(self, sent: str, options: List[str]) -> str:
        ranked = self.rank(sent, options)
        best_word = ranked[0]
        return sent.replace(self.mask_token, best_word)
