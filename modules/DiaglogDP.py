import torch
from modules.Layer import _model_var, pad_sequence
import numpy as np
import torch.nn.functional as F
from data.Vocab import *


class DiaglogDP(object):
    def __init__(self, global_encoder, state_encoder, structured_encoder, decoder, config):
        self.training = False
        self.use_cuda = next(filter(lambda p: p.requires_grad, decoder.parameters())).is_cuda

        self.config = config
        self.global_encoder = global_encoder
        self.state_encoder = state_encoder
        self.structured_encoder = structured_encoder
        self.decoder = decoder

    def train(self):
        self.global_encoder.train()
        self.state_encoder.train()
        self.structured_encoder.train()
        self.decoder.train()
        self.training = True

    def eval(self):
        self.global_encoder.eval()
        self.state_encoder.eval()
        self.structured_encoder.eval()
        self.decoder.eval()
        self.training = False

    '''
    def forward(self, word_indexs, extword_indexs, word_lengths, edu_lengths, arc_masks, feats, gold_arcs, gold_rels):
        if self.use_cuda:
            word_indexs = word_indexs.cuda()
            arc_masks = arc_masks.cuda()
            extword_indexs = extword_indexs.cuda()
            feats = feats.cuda()

        edu_represents, edu_outputs = self.global_encoder(word_indexs, extword_indexs, word_lengths, edu_lengths)

        cur_step = 0
        arc_logits = []
        rel_logits = []

        while not self.is_finished(cur_step, edu_lengths):
            last_arc_index, rel_indexs = self.prepare(cur_step, edu_lengths, gold_arcs, gold_rels)
            cur_feats = feats[:, cur_step, :, :]
            cur_arc_masks = arc_masks[:, cur_step, :]
            last_edu_represent = edu_represents[:, cur_step - 1, :]
            structured_hidden = self.structured_encoder(last_edu_represent, rel_indexs, last_arc_index, edu_lengths)
            state_hidden = self.state_encoder(structured_hidden, edu_represents, edu_outputs, cur_step, edu_lengths, cur_feats)
            arc_logit, rel_logit = self.decoder(state_hidden, cur_arc_masks)
            arc_logits.append(arc_logit.unsqueeze(1))
            rel_logits.append(rel_logit.unsqueeze(1))
            cur_step += 1

        self.arc_logits = torch.cat(arc_logits, dim=1)
        self.rel_logits = torch.cat(rel_logits, dim=1)
    '''
    def forward(self, batch_input_ids, batch_token_type_ids, batch_attention_mask, token_lengths,
                edu_lengths, arc_masks, feats):
        if self.use_cuda:
            batch_input_ids = batch_input_ids.cuda()
            batch_token_type_ids = batch_token_type_ids.cuda()
            batch_attention_mask = batch_attention_mask.cuda()

            arc_masks = arc_masks.cuda()
            feats = feats.cuda()

        bert_outputs, gru_outputs = self.global_encoder(batch_input_ids, batch_token_type_ids, batch_attention_mask, edu_lengths)
        _, _, arc_logits, rel_logits = self.decode(bert_outputs, gru_outputs, edu_lengths, arc_masks, feats)

        self.arc_logits = torch.cat(arc_logits, dim=1)
        self.rel_logits = torch.cat(rel_logits, dim=1)

    def prepare(self, cur_step, edu_lengths, batch_arcs, batch_rels):
        batch_size = len(edu_lengths)

        batch_last_arc = np.ones(batch_size) * Vocab.ROOT
        batch_last_rel = np.ones(batch_size) * Vocab.ROOT

        if not(cur_step == 0 or cur_step == 1):
            for idx in range(batch_size):
                arcs = batch_arcs[idx]
                rels = batch_rels[idx]
                edu_len = edu_lengths[idx]
                last_step = cur_step - 1
                if last_step >= 0 and cur_step < edu_len:
                    last_arc = arcs[last_step]
                    last_rel = rels[last_step]
                    if last_arc is not -1: batch_last_arc[idx] = last_arc
                    if last_rel is not -1: batch_last_rel[idx] = last_rel

        last_arc_index = torch.tensor(batch_last_arc).type(torch.LongTensor)
        last_rel_index = torch.tensor(batch_last_rel).type(torch.LongTensor)
        if self.use_cuda:
            last_arc_index = last_arc_index.cuda()
            last_rel_index = last_rel_index.cuda()
        return last_arc_index, last_rel_index

    def is_finished(self, cur_step, edu_lengths):
        finished_flag = True
        for edu_length in edu_lengths:
            if cur_step < edu_length:
                finished_flag = False
        return finished_flag

    def parse(self, batch_input_ids, batch_token_type_ids, batch_attention_mask, token_lengths,
              edu_lengths, arc_masks, feats):
        if self.use_cuda:
            batch_input_ids = batch_input_ids.cuda()
            batch_token_type_ids = batch_token_type_ids.cuda()
            batch_attention_mask = batch_attention_mask.cuda()

            arc_masks = arc_masks.cuda()
            feats = feats.cuda()

        bert_outputs, gru_outputs = self.global_encoder(batch_input_ids, batch_token_type_ids, batch_attention_mask, edu_lengths)

        pred_arcs, pred_rels, _, _ = self.decode(bert_outputs, gru_outputs, edu_lengths, arc_masks, feats)
        return pred_arcs, pred_rels

    def decode(self, bert_outputs, gru_outputs, edu_lengths, arc_masks, feats):
        cur_step = 0
        arc_logits = []
        rel_logits = []
        pred_arcs = None
        pred_rels = None
        while not self.is_finished(cur_step, edu_lengths):
            last_arc_index, last_rel_index = self.prepare(cur_step, edu_lengths, pred_arcs, pred_rels)
            cur_feats = feats[:, cur_step, :, :]
            cur_arc_masks = arc_masks[:, cur_step, :]

            if cur_step - 1 >= 0:
                last_edu_represent = bert_outputs[:, cur_step - 1, :]
            else:
                last_edu_represent = None
            s_hidden = self.structured_encoder(cur_step, last_edu_represent, last_arc_index, last_rel_index, edu_lengths)

            state_hidden = self.state_encoder(cur_step, s_hidden, bert_outputs, gru_outputs, edu_lengths, cur_feats)
            arc_logit, rel_logit = self.decoder(state_hidden, cur_arc_masks)
            arc_logits.append(arc_logit.unsqueeze(1))
            rel_logits.append(rel_logit.unsqueeze(1))

            pred_arc = arc_logit.detach().max(-1)[1].cpu().numpy()
            batch_size, max_edu_size, label_size = rel_logit.size()
            rel_probs = _model_var(self.decoder, torch.zeros(batch_size, label_size))
            for batch_index, (logits, arc) in enumerate(zip(rel_logit, pred_arc)):
                rel_probs[batch_index] = logits[arc]
            pred_rel = rel_probs.detach().max(-1)[1].cpu().numpy()

            pred_arc = pred_arc.reshape(batch_size, 1)
            pred_rel = pred_rel.reshape(batch_size, 1)

            if cur_step == 0:
                pred_arcs = pred_arc
                pred_rels = pred_rel
            else:
                pred_arcs = np.concatenate((pred_arcs, pred_arc), axis=-1)
                pred_rels = np.concatenate((pred_rels, pred_rel), axis=-1)
            cur_step += 1

        return pred_arcs, pred_rels, arc_logits, rel_logits

    def compute_loss(self, gold_arcs, gold_rels):
        batch_size, max_edu_size, _ = self.arc_logits.size()
        gold_arcs = _model_var(self.decoder, pad_sequence(gold_arcs,
                                                          length=max_edu_size, padding=-1, dtype=np.int64))
        batch_size, max_edu_size, _ = self.arc_logits.size()

        arc_loss = F.cross_entropy(self.arc_logits.view(batch_size * max_edu_size, -1),
                                   gold_arcs.view(-1), ignore_index=-1)

        _, _, _, label_size = self.rel_logits.size()
        rel_logits = _model_var(self.decoder, torch.zeros(batch_size, max_edu_size, label_size))
        for batch_index, (logits, arcs) in enumerate(zip(self.rel_logits, gold_arcs)):
            rel_probs = []
            for i in range(max_edu_size):
                rel_probs.append(logits[i][int(arcs[i])])
            rel_probs = torch.stack(rel_probs, dim=0)
            rel_logits[batch_index] = torch.squeeze(rel_probs, dim=1)

        gold_rels = _model_var(self.decoder, pad_sequence(gold_rels,
                                                          length=max_edu_size, padding=-1, dtype=np.int64))

        rel_loss = F.cross_entropy(rel_logits.view(batch_size * max_edu_size, -1),
                                   gold_rels.view(-1), ignore_index=-1)

        return arc_loss + rel_loss

    def compute_accuracy(self, gold_arcs, gold_rels):
        arc_correct, arc_total, rel_correct = 0, 0, 0
        pred_arcs = self.arc_logits.detach().max(2)[1].cpu().numpy()
        assert len(pred_arcs) == len(gold_arcs)

        batch_idx = 0
        for p_arcs, g_arcs in zip(pred_arcs, gold_arcs):
            edu_len = len(g_arcs)
            for idx in range(edu_len):
                if idx == 0: continue
                if p_arcs[idx] == g_arcs[idx]:
                    arc_correct += 1
                arc_total += 1
            batch_idx += 1

        batch_size, max_edu_size, _, label_size = self.rel_logits.size()

        gold_arcs_index = _model_var(self.decoder, pad_sequence(gold_arcs,
                                                                length=max_edu_size,
                                                                padding=-1, dtype=np.int64))
        rel_logits = _model_var(self.decoder, torch.zeros(batch_size, max_edu_size, label_size))
        for batch_index, (logits, arcs) in enumerate(zip(self.rel_logits, gold_arcs_index)):
            rel_probs = []
            for i in range(max_edu_size):
                rel_probs.append(logits[i][int(arcs[i])])
            rel_probs = torch.stack(rel_probs, dim=0)
            rel_logits[batch_index] = torch.squeeze(rel_probs, dim=1)
        pred_rels = rel_logits.detach().max(2)[1].cpu().numpy()

        assert len(pred_rels) == len(gold_rels)
        batch_idx = 0
        for p_rels, g_rels in zip(pred_rels, gold_rels):
            edu_len = len(g_rels)
            for idx in range(edu_len):
                if idx == 0: continue
                if p_rels[idx] == g_rels[idx]:
                    rel_correct += 1
            batch_idx += 1

        return arc_correct, arc_total, rel_correct
