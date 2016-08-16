# this uses CloudBioLinux's pre-cleaned and merged COSMIC VCF:
# https://github.com/chapmanb/cloudbiolinux/blob/master/utils/prepare_cosmic.py
wget https://s3.amazonaws.com/biodata/variants/cosmic-v67_20131024-hg19.vcf.gz
mv cosmic-v67_20131024-hg19.vcf.gz hg19.cosmic.v67.20131024.gz
tabix -p hg19.cosmic.v67.20131024.gz
