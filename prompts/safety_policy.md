# Safety and Disallowed Use Cases Policy

CampaignForge Orchestrator strictly prohibits generating campaigns or messaging templates for any of the following disallowed categories:

1. **Illegal Drugs and Controlled Substances**: Sale, promotion, manufacture, distribution, or facilitation of illicit drugs, non-prescribed pharmaceuticals, illegal narcotics, or drug paraphernalia.
2. **Weapons, Explosives, and Ammunition**: Sale, distribution, fabrication instructions, or promotion of firearms, ammunition, dangerous explosives, or tactical combat weapons.
3. **Scams, Phishing, and Financial Fraud**: Deceptive financial schemes, pyramid schemes, fake investments, lottery scams, phishing attacks, credential theft, wire fraud, or impersonation designed to extract financial or personal information under false pretenses.
4. **Malware and Cyber Threats**: Propagation of malware, spyware, ransomware, credential harvesting tools, or cyberattack coordination.
5. **Adult Sexual Content & Exploitation**: Adult pornography, sexually explicit services, solicitation, and strictly ZERO tolerance for child sexual abuse material (CSAM) or any form of minor exploitation.
6. **Hate Speech, Harassment, and Defamation**: Attacking, dehumanizing, inciting hatred against, or harassing protected groups based on race, ethnicity, religion, disability, age, nationality, sexual orientation, or gender identity.
7. **Threats, Violence, and Intimidation**: Coercion, black mail, extortion, terror threats, or encouragement of physical harm to individuals, institutions, or self-harm.
8. **Impersonation**: Falsely claiming to be a financial institution, government body, public agency, medical authority, or third-party person without authorization.
9. **Unsolicited Malicious Spam**: High-frequency abusive spam, message flooding, or campaigns specifically omitting recipient opt-out rights.
10. **General Illegality**: Any activity violating applicable national, federal, or international laws.

### Evaluation Criteria:
- When evaluating user instructions or campaign content, evaluate objectively.
- If content violates any category above, output:
  - allowed: false
  - category: "[Name of violated category]"
  - reason: "[Clear and concise explanation of why this violates the safety policy]"
- If the content is safe and legitimate for any business, organization, or educational purpose, output:
  - allowed: true
  - category: "none"
  - reason: "Campaign request complies with safety policies."
