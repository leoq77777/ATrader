import io
import json
import unittest
from unittest.mock import patch
from atrader.providers import provider_details, model_configuration_id
from atrader.ratchet import call_deepseek_llm


class ProviderIdentityTest(unittest.TestCase):
    def setUp(self):
        self.cfg={'llm_base_url':'https://api.deepseek.com','llm_model':'deepseek-chat',
                  'llm_api_key':'test-key','market_data_source':'yahoo'}

    def test_alias_is_not_misrepresented_as_a_version(self):
        item={'model_configuration_id':model_configuration_id(self.cfg),'model_reported':'deepseek-chat',
              'system_fingerprint':'fp_test','timestamp':123}
        identity=provider_details(self.cfg,{'generations':[item]})
        self.assertEqual(identity['paper_account']['provider'],'Alpaca Paper Trading')
        self.assertEqual(identity['model']['provider'],'DeepSeek')
        self.assertIsNone(identity['model']['version'])
        self.assertEqual(identity['model']['system_fingerprint'],'fp_test')

    def test_specific_model_version_and_changed_configuration(self):
        item={'model_configuration_id':model_configuration_id(self.cfg),'model_reported':'deepseek-v3.2','timestamp':123}
        self.assertEqual(provider_details(self.cfg,{'generations':[item]})['model']['version'],'v3.2')
        changed=dict(self.cfg,llm_model='other-model')
        self.assertIsNone(provider_details(changed,{'generations':[item]})['model']['reported_model'])
        self.assertIsNone(provider_details(dict(self.cfg,llm_base_url='https://proxy.example'),{'generations':[item]})['model']['reported_model'])

    def test_endpoint_credentials_and_query_are_never_exposed(self):
        cfg=dict(self.cfg,llm_base_url='https://user:private-password@proxy.example/v1?key=private-query')
        details=provider_details(cfg,{})
        self.assertEqual(details['model']['endpoint'],'https://proxy.example')
        self.assertNotIn('private',json.dumps(details))
        self.assertEqual(details['model']['provider'],'proxy.example')

    def test_successful_response_preserves_actual_model_identity(self):
        response={'model':'deepseek-v3.2','system_fingerprint':'backend-123',
                  'choices':[{'message':{'content':'OK'}}], 'usage':{'prompt_tokens':10,'completion_tokens':1}}
        with patch('atrader.ratchet.urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())):
            text,usage=call_deepseek_llm('system','user',self.cfg)
        self.assertEqual(text,'OK')
        self.assertEqual(usage['model_reported'],'deepseek-v3.2')
        self.assertEqual(usage['model_configuration_id'],model_configuration_id(self.cfg))
        self.assertEqual(usage['system_fingerprint'],'backend-123')
