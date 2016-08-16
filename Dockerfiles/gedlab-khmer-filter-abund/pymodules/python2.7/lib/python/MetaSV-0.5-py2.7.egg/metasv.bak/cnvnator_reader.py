import logging
import re
import os

import vcf

from sv_interval import SVInterval

logger = logging.getLogger(__name__)

mydir = os.path.dirname(os.path.realpath(__file__))

'''
From the CNVnator README
The output is as follows:

CNV_type coordinates CNV_size normalized_RD e-val1 e-val2 e-val3 e-val4 q0

normalized_RD -- normalized to 1.
e-val1        -- is calculated using t-test statistics.
e-val2        -- is from probability of RD values within the region to be in
                 the tails of gaussian distribution describing frequencies of RD values in bins.
e-val3        -- same as e-val1 but for the middle of CNV
e-val4        -- same as e-val2 but for the middle of CNV
q0            -- fraction of reads mapped with q0 quality
'''

pattern = re.compile("(:|-)")
cnvnator_name = 'CNVnator'
cnvnator_source = set([cnvnator_name])

sv_type_dict = {"deletion": "DEL", "duplication": "DUP"}


class CNVnatorRecord:
    def __init__(self, record_string):
        fields = record_string.split()
        self.sv_type = sv_type_dict[fields[0]]
        self.name = cnvnator_name

        coordinates = pattern.split(fields[1])
        self.chromosome = coordinates[0]
        self.start = int(coordinates[2])
        self.end = int(coordinates[4])
        self.sv_len = self.end - self.start
        self.normalized_rd = float(fields[3])
        self.e_val1 = float(fields[4])
        self.e_val2 = float(fields[5])
        self.e_val3 = float(fields[6])
        self.e_val4 = float(fields[7])
        self.q0 = float(fields[8])

        self.info = {
            "CN_NORMALIZED_RD": self.normalized_rd,
            "CN_EVAL1": self.e_val1,
            "CN_EVAL2": self.e_val2,
            "CN_EVAL3": self.e_val3,
            "CN_EVAL4": self.e_val4,
            "CN_Q0": self.q0
        }

    def __str__(self):
        return str(self.__dict__)

    def to_sv_interval(self):
        if self.sv_type not in CNVnatorReader.svs_supported:
            return None

        return SVInterval(self.chromosome,
                          self.start,
                          self.end,
                          name=self.name,
                          sv_type=self.sv_type,
                          length=self.sv_len,
                          sources=cnvnator_source,
                          info=self.info,
                          native_sv=self)

    def to_vcf_record(self, sample):
        alt = [vcf.model._SV(self.sv_type)]
        sv_len = -self.sv_len if self.sv_type == "DEL" else self.sv_len
        info = {"SVLEN": sv_len,
                "SVTYPE": self.sv_type}
        if self.sv_type == "DEL" or self.sv_type == "DUP":
            info["END"] = self.start + self.sv_len
        else:
            return None

        info.update(self.info)

        vcf_record = vcf.model._Record(self.chromosome,
                                       self.start - 1,
                                       ".",
                                       "N",
                                       alt,
                                       ".",
                                       ".",
                                       info,
                                       "GT",
                                       [0],
                                       [vcf.model._Call(None, sample, vcf.model.make_calldata_tuple("GT")(GT="1/1"))])
        return vcf_record


class CNVnatorReader:
    svs_supported = set(["DEL", "DUP"])

    def __init__(self, file_name, reference_handle=None, svs_to_report=None):
        logger.info("File is " + file_name)
        self.file_fd = open(file_name)
        self.reference_handle = reference_handle
        self.svs_supported = CNVnatorReader.svs_supported
        if svs_to_report is not None:
            self.svs_supported &= set(svs_to_report)

    def __iter__(self):
        return self

    def next(self):
        while True:
            line = self.file_fd.next()
            if line[0] != "#":
                record = CNVnatorRecord(line.strip())
                if record.sv_type in self.svs_supported:
                    return record
