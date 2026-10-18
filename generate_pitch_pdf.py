import os
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
)
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Group, Polygon

def build_pdf():
    pdf_path = "crm_automation_agent_pitch.pdf"
    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()

    # Custom palette
    PRIMARY = colors.HexColor("#1E3A8A")    # Deep Navy
    SECONDARY = colors.HexColor("#2563EB")  # Royal Blue
    ACCENT = colors.HexColor("#0D9488")     # Teal
    DARK = colors.HexColor("#0F172A")       # Slate 900
    MUTED = colors.HexColor("#475569")      # Slate 600
    BG_LIGHT = colors.HexColor("#F8FAFC")   # Slate 50
    BORDER_COLOR = colors.HexColor("#CBD5E1")

    # Typography styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=28,
        textColor=PRIMARY,
        spaceAfter=4
    )

    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=11,
        leading=15,
        textColor=MUTED,
        spaceAfter=14
    )

    h1_style = ParagraphStyle(
        'H1',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=19,
        textColor=PRIMARY,
        spaceBefore=12,
        spaceAfter=6
    )

    h2_style = ParagraphStyle(
        'H2',
        parent=styles['Heading3'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=SECONDARY,
        spaceBefore=8,
        spaceAfter=4
    )

    body_style = ParagraphStyle(
        'Body',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=13.5,
        textColor=DARK,
        spaceAfter=6
    )

    body_bold = ParagraphStyle(
        'BodyBold',
        parent=body_style,
        fontName='Helvetica-Bold'
    )

    callout_style = ParagraphStyle(
        'Callout',
        parent=body_style,
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#1E293B")
    )

    code_style = ParagraphStyle(
        'Code',
        parent=styles['Normal'],
        fontName='Courier',
        fontSize=8.5,
        leading=11.5,
        textColor=colors.HexColor("#0F172A")
    )

    chat_user = ParagraphStyle(
        'ChatUser',
        parent=body_style,
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#1D4ED8")
    )

    chat_agent = ParagraphStyle(
        'ChatAgent',
        parent=body_style,
        fontSize=9,
        leading=12.5,
        textColor=colors.HexColor("#0F172A")
    )

    story = []

    # ---------------- HEADER / HERO ----------------
    story.append(Paragraph("CampaignForge Orchestrator", title_style))
    story.append(Paragraph("Autonomous Multi-Turn Agentic CRM Messaging & Workflow Architecture | Pitch & Capability Guide", subtitle_style))

    # Executive Summary Card
    summary_text = """
    <b>Executive Overview:</b> CampaignForge is an adaptive, non-hardcoded AI agent that transforms raw business concepts into production-grade CRM messaging campaigns. Built on zero-hardcoded dynamic discovery, the agent autonomously determines domain requirements, cleans and maps contact lists (with phone normalization & suppression), proposes context-authentic message tones, engineers customized pipeline execution plans, and generates strictly compliant messaging sequences with verified personalization tag replacements.
    """
    summary_table = Table(
        [[Paragraph(summary_text, callout_style)]],
        colWidths=[540]
    )
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor("#EFF6FF")),
        ('BORDER', (0,0), (-1,-1), 1, colors.HexColor("#BFDBFE")),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LEFTPADDING', (0,0), (-1,-1), 10),
        ('RIGHTPADDING', (0,0), (-1,-1), 10),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 10))

    # ---------------- SECTION 1: HOW THE AGENT WORKS ----------------
    story.append(Paragraph("1. Core Architectural Pillars & How The Agent Works", h1_style))
    
    pillars_data = [
        [
            Paragraph("<b>Pillar</b>", body_bold),
            Paragraph("<b>Agent Mechanism</b>", body_bold),
            Paragraph("<b>Business Value</b>", body_bold)
        ],
        [
            Paragraph("<b>Zero Hardcoding</b>", body_style),
            Paragraph("Slots, tones, questions, and workflows are dynamically derived at runtime by the LLM from the user's specific domain (Retail, Dental, SaaS, NGOs, Education). Prompts reside in editable markdown files.", body_style),
            Paragraph("Infinitely adaptable across industries with zero code rewrites.", body_style)
        ],
        [
            Paragraph("<b>Pluggable Ingestion & Sanitization</b>", body_style),
            Paragraph("Auto-detects Name, Phone, and arbitrary columns. Enforces E.164 phone normalization, deduplication, suppression filtering, and dismisses invalid files.", body_style),
            Paragraph("Clean data hygiene, zero delivery failures, and compliance with DNC registries.", body_style)
        ],
        [
            Paragraph("<b>Durable Workspace Memory</b>", body_style),
            Paragraph("Persistent SQLite storage tracks brand identity, past campaigns, preferred tones, and rules across sessions.", body_style),
            Paragraph("Never repeats questions; builds contextual brand intelligence over time.", body_style)
        ],
        [
            Paragraph("<b>Dual-Layer Safety & Governance</b>", body_style),
            Paragraph("Code-level LLM classification guardrails evaluate all requests and templates against a strict safety policy, failing closed on violations.", body_style),
            Paragraph("Brand protection, ethical compliance, and anti-abuse enforcement.", body_style)
        ],
        [
            Paragraph("<b>Strict Compliance & Self-Repair</b>", body_style),
            Paragraph("Word limit ceiling (≤320 words), mandatory opt-out text, business naming, and tag validation with automatic repair loops.", body_style),
            Paragraph("Guaranteed deliverability under telecom regulatory limits (TCPA, TRAI, GDPR).", body_style)
        ]
    ]

    pillars_table = Table(pillars_data, colWidths=[110, 270, 160])
    pillars_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#F1F5F9")),
        ('GRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    story.append(pillars_table)
    story.append(Spacer(1, 12))

    # ---------------- SECTION 2: WORKFLOW DIAGRAM ----------------
    story.append(Paragraph("2. End-to-End Orchestrator Workflow Diagram", h1_style))
    story.append(Paragraph("Visual representation of the agent's deterministic, multi-turn lifecycle pipeline:", body_style))

    # Vector diagram
    d = Drawing(540, 115)
    
    stages = [
        ("1. INGESTION", "File / Paste Parse", "E.164 & DNC Check", colors.HexColor("#0284C7")),
        ("2. DISCOVERY", "Adaptive Slotting", "Memory Inquiry", colors.HexColor("#2563EB")),
        ("3. TONE LAB", "Dynamic Tones", "Real Previews", colors.HexColor("#7C3AED")),
        ("4. PLANNER", "Domain Pipeline", "Event Emission", colors.HexColor("#0D9488")),
        ("5. TEMPLATES", "Cadence Draft", "Compliance/Repair", colors.HexColor("#059669"))
    ]

    box_w = 95
    box_h = 75
    gap = 16
    start_x = 0

    for i, (stg_num, line1, line2, color_accent) in enumerate(stages):
        x = start_x + i * (box_w + gap)
        # Main box
        d.add(Rect(x, 15, box_w, box_h, rx=6, ry=6, fillColor=colors.HexColor("#F8FAFC"), strokeColor=color_accent, strokeWidth=1.5))
        # Top banner
        d.add(Rect(x, 15 + box_h - 18, box_w, 18, rx=6, ry=6, fillColor=color_accent, strokeColor=color_accent, strokeWidth=1))
        # Banner text
        d.add(String(x + box_w/2, 15 + box_h - 13, stg_num, fontName="Helvetica-Bold", fontSize=8, textAnchor="middle", fillColor=colors.white))
        # Body text
        d.add(String(x + box_w/2, 50, line1, fontName="Helvetica-Bold", fontSize=7.5, textAnchor="middle", fillColor=DARK))
        d.add(String(x + box_w/2, 35, line2, fontName="Helvetica", fontSize=7, textAnchor="middle", fillColor=MUTED))
        
        # Draw connector arrow
        if i < len(stages) - 1:
            arr_x = x + box_w + 3
            arr_y = 15 + box_h/2
            d.add(Line(arr_x, arr_y, arr_x + 10, arr_y, strokeColor=colors.HexColor("#94A3B8"), strokeWidth=2))
            d.add(Polygon([arr_x + 10, arr_y + 3, arr_x + 14, arr_y, arr_x + 10, arr_y - 3], fillColor=colors.HexColor("#94A3B8"), strokeColor=colors.HexColor("#94A3B8")))

    # Bottom status bar in diagram
    d.add(Rect(0, 0, 540, 10, rx=3, ry=3, fillColor=colors.HexColor("#F1F5F9"), strokeColor=colors.transparent))
    d.add(String(270, 2, "Full Deterministic Lifecycle: <pipeline_event> SSE Streaming | Dual Safety Guards | SQLite & JSONL Mirrors", fontName="Helvetica", fontSize=6.5, textAnchor="middle", fillColor=MUTED))

    story.append(d)
    story.append(Spacer(1, 14))

    # Page Break to ensure Clean Page 2 for Conversation & Orchestrations
    story.append(PageBreak())

    # ---------------- SECTION 3: STEP-BY-STEP ORCHESTRATIONS POSSIBLE ----------------
    story.append(Paragraph("3. Full Capabilities & Orchestrations Possible by the Agent", h1_style))

    orch_data = [
        [Paragraph("<b>Pipeline Stage</b>", body_bold), Paragraph("<b>Autonomous Actions Executed by Agent</b>", body_bold)],
        [
            Paragraph("<b>Stage 1: Contact Intake & Hygiene</b>", body_style),
            Paragraph("• Pluggable CSV, XLSX, TSV, and pasted lines parsing.<br/>• Automatic column header detection (Name, Phone, custom fields).<br/>• Normalization to E.164 (+977..., +1...) via Google phonenumbers library.<br/>• Deduplication by phone number; suppression database lookup against <code>data/suppression.json</code>.<br/>• Immediate rejection of corrupted files with 0 valid numbers.", body_style)
        ],
        [
            Paragraph("<b>Stage 2: Adaptive Discovery & Memory</b>", body_style),
            Paragraph("• Domain classification (Retail, Healthcare, NGO, B2B SaaS, Education).<br/>• Dynamic slot extraction: business name, campaign goal, audience, CTA, link, messages count, cadence.<br/>• Memory integration: Proactively checks past workspace campaigns and asks whether to reference past donations/actions.<br/>• 1-2 focused questions at a time; states assumptions when user says 'you decide'.", body_style)
        ],
        [
            Paragraph("<b>Stage 3: Tone Exploration & Agreement</b>", body_style),
            Paragraph("• Proposes 3-5 domain-tailored candidate tones (e.g. 'Gentle & Reassuring' for clinics; 'High-Energy Hype' for retail).<br/>• Crafts live preview examples with real campaign details.<br/>• Supports user refinement loops (e.g. 'make it punchier' or 'less formal') until locked in.", body_style)
        ],
        [
            Paragraph("<b>Stage 4: Dynamic Planning & Event Streaming</b>", body_style),
            Paragraph("• Synthesizes customized orchestrator steps tailored to the exact campaign.<br/>• Emits standard <code>&lt;pipeline_event&gt;</code> SSE events driving live animated timeline UI.<br/>• Status progression: <code>active</code> → <code>processing</code> → <code>done</code> → <code>waiting_for_input</code>.", body_style)
        ],
        [
            Paragraph("<b>Stage 5: Compliant Template Generation</b>", body_style),
            Paragraph("• Generates multi-touch message sequence (Announcement → Follow-up → Final Call).<br/>• Strict ceiling validation (≤320 words). Auto-regenerates with repair prompt if breached.<br/>• Guaranteed mandatory opt-out ending ('Reply STOP to unsubscribe').<br/>• Real recipient previews replacing <code>{{customer_name}}</code>, <code>{{phone}}</code>, and custom row columns.<br/>• Individual message editing (<code>✏️ Edit</code>) and LLM-assisted regeneration (<code>🔄 Regenerate</code>).", body_style)
        ],
        [
            Paragraph("<b>Stage 6: Enterprise Audit & Export</b>", body_style),
            Paragraph("• SQLite relational database mirror + append-only JSONL files.<br/>• Full prompt, raw response, token usage, latency (ms), and safety verification logs.<br/>• One-click export of approved Campaign Package JSON & Full Session Run Log.", body_style)
        ]
    ]

    orch_table = Table(orch_data, colWidths=[160, 380])
    orch_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#F1F5F9")),
        ('GRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    story.append(orch_table)
    story.append(Spacer(1, 14))

    # ---------------- SECTION 4: EXAMPLE INPUT CONVERSATION ----------------
    story.append(Paragraph("4. Real-World End-to-End Example Conversation", h1_style))
    story.append(Paragraph("Below is an actual conversation script demonstrating memory recall, contact integration, and template generation:", body_style))

    chat_data = [
        [
            Paragraph("<b>Turn</b>", body_bold),
            Paragraph("<b>Speaker</b>", body_bold),
            Paragraph("<b>Dialogue & Agent Output</b>", body_bold)
        ],
        [
            Paragraph("1", body_style),
            Paragraph("<b>User</b>", chat_user),
            Paragraph("<i>\"Hello! We want to run a follow-up campaign for flood relief in Nepal.\"</i>", body_style)
        ],
        [
            Paragraph("2", body_style),
            Paragraph("<b>Agent</b>", chat_agent),
            Paragraph("<b>Welcome back!</b> I see that <b>Hope for Nepal Foundation</b> previously ran the <i>Nepal Flood Relief Fund</i> with us.<br/><br/>Before we draft the sequence: <b>Would you like this new message to specifically acknowledge past donor contributions</b> (e.g. <i>'You donated last time for Nepal flood relief and your support saved lives—will you stand with us once more?'</i>)? Also, please upload your donor list so I can personalize names.", body_style)
        ],
        [
            Paragraph("3", body_style),
            Paragraph("<b>User</b>", chat_user),
            Paragraph("<i>*Uploads <code>mock_contacts_nepal_100.csv</code>*<br/>\"Yes, definitely mention their past support. Goal is $50 emergency medical kits. Suggested gap is 3 days.\"</i>", body_style)
        ],
        [
            Paragraph("4", body_style),
            Paragraph("<b>Agent</b>", chat_agent),
            Paragraph("<b>[OK] 105 clean contacts sanitized & ready</b> (0 duplicates, 0 suppressed).<br/><b>* Campaign Configured:</b> Hope for Nepal Foundation | Goal: $50 Medical Kits | Past donor reference: Enabled.<br/><br/><b>Candidate Tones Proposed:</b><br/>• <b>[Tone 1] Urgent & Dignified</b>: <i>'Dear {{customer_name}}, you helped us provide emergency aid during the last floods. As monsoons return, 120 families need medical kits...'</i><br/>• <b>[Tone 2] Community Solidarity</b>: <i>'Hi {{customer_name}}, collective action saved lives in Nepal last year...'</i><br/><br/>Please select a tone to plan the pipeline.", body_style)
        ],
        [
            Paragraph("5", body_style),
            Paragraph("<b>User</b>", chat_user),
            Paragraph("<i>*Clicks \"Urgent & Dignified\"*</i>", body_style)
        ],
        [
            Paragraph("6", body_style),
            Paragraph("<b>Agent</b>", chat_agent),
            Paragraph("<b>* Pipeline Planned & Executed:</b><br/>[OK] Sequence structured (Day 1 -> Day 4 -> Day 7)<br/>[OK] 3 compliant templates drafted (<= 320 words, opt-out verified)<br/><br/><b>Live Recipient Preview (Message 1):</b><br/><b>* Ramesh Adhikari</b> (+97......0000 | City: Kathmandu | Tier: Supporter):<br/><i>\"Dear Ramesh Adhikari, we hope this finds you well. As a valued supporter of Hope for Nepal Foundation, your past generosity brought relief to families in need. Monsoons have once again displaced communities, and we urgently need $50 medical supply kits... Reply STOP to unsubscribe.\"</i><br/><br/>[Edit] [Regenerate] [Approve Campaign]", body_style)
        ]
    ]

    chat_table = Table(chat_data, colWidths=[35, 65, 440])
    chat_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#F1F5F9")),
        ('GRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BACKGROUND', (0,1), (-1,1), colors.HexColor("#F8FAFC")),
        ('BACKGROUND', (0,3), (-1,3), colors.HexColor("#F8FAFC")),
        ('BACKGROUND', (0,5), (-1,5), colors.HexColor("#F8FAFC")),
    ]))
    story.append(chat_table)
    story.append(Spacer(1, 12))

    # ---------------- FOOTER CALLOUT ----------------
    footer_text = """
    <b>Summary:</b> CampaignForge delivers end-to-end automation without sacrificing human oversight or safety. It replaces static forms with intelligent, contextual conversation and produces audit-ready, compliant campaigns in seconds.
    """
    footer_table = Table(
        [[Paragraph(footer_text, callout_style)]],
        colWidths=[540]
    )
    footer_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor("#F0FDF4")),
        ('BORDER', (0,0), (-1,-1), 1, colors.HexColor("#BBF7D0")),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING', (0,0), (-1,-1), 10),
        ('RIGHTPADDING', (0,0), (-1,-1), 10),
    ]))
    story.append(footer_table)

    doc.build(story)
    print("PDF build complete: crm_automation_agent_pitch.pdf")

if __name__ == "__main__":
    build_pdf()
