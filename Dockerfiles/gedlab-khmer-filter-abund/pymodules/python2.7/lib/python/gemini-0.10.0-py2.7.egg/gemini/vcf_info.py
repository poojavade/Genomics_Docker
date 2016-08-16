import pysam
import sys
import os
import math

class VCFInfo(object):
    
    def __init__(self, vcf_filename):
        self.filename = vcf_filename
        self.fh = open(self.filename)
        

    def _get_header(self):
        """
        return a list of the header lines for a VCF file.
        """
        self.header = []
        self.header_size = 0
        for line in self.fh:
            if not line.startswith("#"):
                break
            else:
                self.header_size += len(line)
                self.header.append(line)
    
    def _get_file_size(self):
        return os.path.getsize(self.filename)

    def _get_first_N_recs(self, N=1000):
        """
        return the first N records of a VCF file
        """
        count = 0
        self.N_lines = []
        for line in self.fh:
            if count < N:
                self.N_lines.append(line)
            else:
                break

    def _get_mean_rec_size(self):
        """
        return the mean record size
        """
        total_bytes = 0
        for line in self.N_lines:
            total_bytes += len(line)
        return float(total_bytes) / float(len(self.N_lines))
    
    def get_header(self):
        return "".join(self.header)
   
    def estimate_number_of_records(self):
        self._get_header()
        self._get_file_size()
        self._get_first_N_recs()
        return int(math.ceil((self._get_file_size() - self.header_size) \
               / self._get_mean_rec_size()))
    
    def close(self):
        self.fh.close()

vcf = VCFInfo(sys.argv[1])
print vcf.estimate_number_of_records()
vcf.close()
